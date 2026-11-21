import asyncio
import io
import json
import os
from typing import Optional

import discord
from discord.ext import commands

DATA_PATH = os.path.join("data", "tickets.json")
COLOR = discord.Color.blurple()
DELETE_DELAY = 10  # seconds before a closed ticket is deleted
NOT_TICKET = "This command can only be used inside a ticket channel."


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class TicketStore:
    """Tiny JSON-backed store for guild config + ticket data."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"guilds": {}, "tickets": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.data.setdefault("guilds", {})
        self.data.setdefault("tickets", {})

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def guild(self, guild_id: int) -> dict:
        return self.data["guilds"].setdefault(
            str(guild_id),
            {
                "category_id": None,
                "os_role_id": None,
                "manager_role_id": None,
                "staff_role_id": None,
                "log_channel_id": None,
                "counter": 0,
            },
        )

    def get_ticket(self, channel_id: int) -> Optional[dict]:
        return self.data["tickets"].get(str(channel_id))

    def set_ticket(self, channel_id: int, ticket: dict):
        self.data["tickets"][str(channel_id)] = ticket
        self.save()

    def delete_ticket(self, channel_id: int):
        if self.data["tickets"].pop(str(channel_id), None) is not None:
            self.save()

    def open_ticket_of(self, guild_id: int, user_id: int) -> Optional[str]:
        for cid, t in self.data["tickets"].items():
            if (
                t["guild_id"] == guild_id
                and t["owner_id"] == user_id
                and t["status"] == "open"
            ):
                return cid
        return None

    def count_open(self, guild_id: int) -> int:
        return sum(
            1
            for t in self.data["tickets"].values()
            if t["guild_id"] == guild_id and t["status"] == "open"
        )


# --------------------------------------------------------------------------- #
# Views (persistent buttons)
# --------------------------------------------------------------------------- #
class PanelView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Create Ticket",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
        custom_id="tickets:create",
    )
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel, error = await self.cog.create_ticket(interaction.guild, interaction.user)
        if error:
            await interaction.followup.send(error, ephemeral=True)
        else:
            await interaction.followup.send(
                f"Your ticket has been created: {channel.mention}", ephemeral=True
            )


class TicketControlView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Claim",
        emoji="🙋",
        style=discord.ButtonStyle.success,
        custom_id="tickets:claim",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await self.cog.do_claim(interaction.channel, interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="tickets:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        error = await self.cog.do_close(interaction.channel, interaction.user)
        await interaction.followup.send(error or "Closing ticket...", ephemeral=True)


class CancelDeleteView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=DELETE_DELAY)
        self.cog = cog
        self.cancelled = False

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.store.guild(interaction.guild.id)
        if not self.cog.is_staff(interaction.user, cfg):
            return await interaction.response.send_message(
                "Only ticket staff can cancel the deletion.", ephemeral=True
            )
        self.cancelled = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                description="Deletion cancelled. The ticket stays closed. "
                "Use `-ticket reopen` to reopen it.",
                color=COLOR,
            ),
            view=None,
        )


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Tickets(commands.Cog, name="Tickets"):
    """Ticket system."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = TicketStore(DATA_PATH)
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self):
        # Re-register persistent buttons so they work after restarts
        self.bot.add_view(PanelView(self))
        self.bot.add_view(TicketControlView(self))

    async def cog_check(self, ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument. Make sure you mention a valid role/channel.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        elif isinstance(error, discord.Forbidden):
            await ctx.send(
                "I'm missing permissions to do that (I need Manage Channels and Manage Roles)."
            )
        else:
            raise error

    # ------------------------------ help page ----------------------------- #
    HELP_LINES = [
        ("`{p}ticket setup` / `{p}setup tickets`", "Create or configure the Tickets category"),
        ("`{p}ticket config`", "View ticket settings"),
        ("`{p}ticket osrole [@role]`", "Set or create an OS role with full ticket access"),
        ("`{p}ticket managerrole @role`", "Set the Ticket Manager role"),
        ("`{p}ticket staffrole @role`", "Set the Ticket Staff role"),
        ("`{p}ticket logchannel #channel`", "Set the private ticket log channel"),
        ("`{p}ticket panel #channel`", "Send the Create a ticket panel"),
        ("`{p}ticket open`", "Create your own ticket"),
        ("`{p}ticket claim`", "Claim the current ticket"),
        ("`{p}ticket close`", "Close and delete the current ticket"),
        ("`{p}ticket reopen`", "Reopen the current ticket"),
        ("`{p}ticket info`", "View ticket information"),
        ("`{p}ticket transcript`", "Download the ticket message transcript"),
    ]

    # Picked up by the help menu in general.py ({p} becomes the bot prefix)
    help_text = "# Tickets\n\n" + "\n".join(f"{u} - {d}" for u, d in HELP_LINES)

    # ----------------------------- helpers -------------------------------- #
    @staticmethod
    def role_ids(cfg: dict) -> set:
        return {
            cfg[k]
            for k in ("os_role_id", "manager_role_id", "staff_role_id")
            if cfg.get(k)
        }

    def is_staff(self, member: discord.Member, cfg: dict) -> bool:
        if member.guild_permissions.administrator:
            return True
        ids = self.role_ids(cfg)
        return any(r.id in ids for r in member.roles)

    def build_overwrites(
        self,
        guild: discord.Guild,
        cfg: dict,
        owner: Optional[discord.Member] = None,
    ) -> dict:
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
            ),
        }
        staff = guild.get_role(cfg["staff_role_id"]) if cfg.get("staff_role_id") else None
        manager = guild.get_role(cfg["manager_role_id"]) if cfg.get("manager_role_id") else None
        osr = guild.get_role(cfg["os_role_id"]) if cfg.get("os_role_id") else None

        if staff:
            ow[staff] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        if manager:
            ow[manager] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True,
            )
        if osr:  # full ticket access
            ow[osr] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True,
                manage_channels=True,
            )
        if owner:
            ow[owner] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        return ow

    async def refresh_category(self, guild: discord.Guild):
        """Re-apply category permissions after a role change."""
        cfg = self.store.guild(guild.id)
        category = guild.get_channel(cfg["category_id"]) if cfg.get("category_id") else None
        if isinstance(category, discord.CategoryChannel):
            try:
                await category.edit(
                    overwrites=self.build_overwrites(guild, cfg),
                    reason="Ticket roles updated",
                )
            except discord.HTTPException:
                pass

    async def send_log(self, guild: discord.Guild, embed: discord.Embed, file=None):
        cfg = self.store.guild(guild.id)
        ch = guild.get_channel(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed, file=file)
            except discord.HTTPException:
                pass

    async def transcript_bytes(self, channel: discord.TextChannel) -> bytes:
        lines = [
            f"Transcript of #{channel.name} ({channel.id})",
            f"Generated {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "-" * 50,
        ]
        async for m in channel.history(limit=None, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            content = m.clean_content or ""
            if not content and m.embeds:
                content = f"[embed: {m.embeds[0].title or m.embeds[0].description or ''}]"
            for a in m.attachments:
                content += f"\n    [attachment] {a.url}"
            lines.append(f"[{ts}] {m.author} ({m.author.id}): {content}")
        return "\n".join(lines).encode("utf-8")

    # --------------------------- core ticket logic ------------------------ #
    async def create_ticket(self, guild: discord.Guild, member: discord.Member):
        cfg = self.store.guild(guild.id)
        category = guild.get_channel(cfg["category_id"]) if cfg.get("category_id") else None
        if not isinstance(category, discord.CategoryChannel):
            return None, "Tickets aren't set up yet. An admin needs to run `-ticket setup`."

        existing = self.store.open_ticket_of(guild.id, member.id)
        if existing:
            ch = guild.get_channel(int(existing))
            if ch:
                return None, f"You already have an open ticket: {ch.mention}"
            self.store.delete_ticket(int(existing))  # stale entry

        cfg["counter"] += 1
        number = cfg["counter"]
        try:
            channel = await guild.create_text_channel(
                f"ticket-{number:04d}",
                category=category,
                overwrites=self.build_overwrites(guild, cfg, owner=member),
                topic=f"Ticket #{number} | Opened by {member} ({member.id})",
                reason=f"Ticket opened by {member}",
            )
        except discord.Forbidden:
            cfg["counter"] -= 1
            return None, "I don't have permission to create ticket channels."
        except discord.HTTPException:
            cfg["counter"] -= 1
            return None, "Something went wrong creating your ticket. Try again."

        self.store.set_ticket(
            channel.id,
            {
                "guild_id": guild.id,
                "owner_id": member.id,
                "number": number,
                "created_at": int(discord.utils.utcnow().timestamp()),
                "claimed_by": None,
                "status": "open",
                "closed_by": None,
            },
        )

        staff = guild.get_role(cfg["staff_role_id"]) if cfg.get("staff_role_id") else None
        embed = discord.Embed(
            title=f"Ticket #{number:04d}",
            description=(
                f"Welcome {member.mention}! Describe your issue and a staff member "
                "will be with you shortly."
            ),
            color=COLOR,
        )
        await channel.send(
            content=f"{member.mention} {staff.mention if staff else ''}".strip(),
            embed=embed,
            view=TicketControlView(self),
        )

        log = discord.Embed(title="Ticket Opened", color=discord.Color.green())
        log.add_field(name="Ticket", value=channel.mention)
        log.add_field(name="Opened by", value=f"{member.mention} (`{member.id}`)")
        await self.send_log(guild, log)
        return channel, None

    async def do_claim(self, channel, member):
        t = self.store.get_ticket(channel.id)
        if not t:
            return False, NOT_TICKET
        cfg = self.store.guild(channel.guild.id)
        if not self.is_staff(member, cfg):
            return False, "Only ticket staff can claim tickets."
        if t["status"] != "open":
            return False, "This ticket is closed."
        if t["claimed_by"]:
            if t["claimed_by"] == member.id:
                return False, "You've already claimed this ticket."
            return False, f"This ticket is already claimed by <@{t['claimed_by']}>."
        t["claimed_by"] = member.id
        self.store.set_ticket(channel.id, t)
        await channel.send(
            embed=discord.Embed(
                description=f"🙋 {member.mention} has claimed this ticket.", color=COLOR
            )
        )
        return True, "Ticket claimed."

    async def do_close(self, channel, closer) -> Optional[str]:
        """Returns an error string, or None on success."""
        t = self.store.get_ticket(channel.id)
        if not t:
            return NOT_TICKET
        cfg = self.store.guild(channel.guild.id)
        if closer.id != t["owner_id"] and not self.is_staff(closer, cfg):
            return "You can't close this ticket."
        if t["status"] == "closed":
            return "This ticket is already closed."

        t["status"] = "closed"
        t["closed_by"] = closer.id
        self.store.set_ticket(channel.id, t)

        # Send transcript to the log channel
        try:
            data = await self.transcript_bytes(channel)
            log = discord.Embed(
                title=f"Ticket #{t['number']:04d} Closed", color=discord.Color.red()
            )
            log.add_field(name="Opened by", value=f"<@{t['owner_id']}>")
            log.add_field(name="Closed by", value=closer.mention)
            log.add_field(
                name="Claimed by",
                value=f"<@{t['claimed_by']}>" if t["claimed_by"] else "Nobody",
            )
            await self.send_log(
                channel.guild,
                log,
                discord.File(io.BytesIO(data), filename=f"transcript-{channel.name}.txt"),
            )
        except discord.HTTPException:
            pass

        # Lock the opener out
        owner = channel.guild.get_member(t["owner_id"])
        if owner:
            try:
                await channel.set_permissions(owner, overwrite=None)
            except discord.HTTPException:
                pass

        view = CancelDeleteView(self)
        await channel.send(
            embed=discord.Embed(
                description=f"🔒 Ticket closed by {closer.mention}. "
                f"This channel will be deleted in {DELETE_DELAY} seconds.",
                color=discord.Color.red(),
            ),
            view=view,
        )
        task = asyncio.create_task(self._delete_after(channel, view))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def _delete_after(self, channel: discord.TextChannel, view: CancelDeleteView):
        await view.wait()
        if view.cancelled:
            return
        t = self.store.get_ticket(channel.id)
        if not t or t["status"] != "closed":  # reopened in the meantime
            return
        try:
            await channel.delete(reason="Ticket closed")
        except discord.HTTPException:
            return
        self.store.delete_ticket(channel.id)

    async def do_reopen(self, channel, member):
        t = self.store.get_ticket(channel.id)
        if not t:
            return False, NOT_TICKET
        cfg = self.store.guild(channel.guild.id)
        if not self.is_staff(member, cfg):
            return False, "Only ticket staff can reopen tickets."
        if t["status"] == "open":
            return False, "This ticket is already open."
        t["status"] = "open"
        t["closed_by"] = None
        self.store.set_ticket(channel.id, t)

        owner = channel.guild.get_member(t["owner_id"])
        if owner:
            await channel.set_permissions(
                owner,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        await channel.send(
            content=owner.mention if owner else None,
            embed=discord.Embed(
                description=f"🔓 Ticket reopened by {member.mention}.",
                color=discord.Color.green(),
            ),
        )
        return True, "Ticket reopened."

    async def run_setup(self, ctx: commands.Context):
        cfg = self.store.guild(ctx.guild.id)
        category = ctx.guild.get_channel(cfg["category_id"]) if cfg.get("category_id") else None
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(ctx.guild.categories, name="Tickets")

        overwrites = self.build_overwrites(ctx.guild, cfg)
        if category is None:
            category = await ctx.guild.create_category(
                "Tickets", overwrites=overwrites, reason=f"Ticket setup by {ctx.author}"
            )
            action = "Created"
        else:
            await category.edit(overwrites=overwrites, reason=f"Ticket setup by {ctx.author}")
            action = "Updated"

        cfg["category_id"] = category.id
        self.store.save()

        embed = discord.Embed(
            title="Tickets Setup",
            description=f"{action} the **{category.name}** category.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Next steps",
            value=(
                "`-ticket staffrole @role`\n"
                "`-ticket managerrole @role`\n"
                "`-ticket osrole [@role]`\n"
                "`-ticket logchannel #channel`\n"
                "`-ticket panel #channel`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    # ------------------------------ commands ------------------------------ #
    @commands.group(
        name="setup",
        invoke_without_command=True,
        help="Create or configure the Tickets category",
    )
    @commands.has_permissions(administrator=True)
    async def setup_group(self, ctx: commands.Context):
        await ctx.send("Usage: `-setup tickets`")

    @setup_group.command(name="tickets", help="Create or configure the Tickets category")
    @commands.has_permissions(administrator=True)
    async def setup_tickets(self, ctx: commands.Context):
        await self.run_setup(ctx)

    @commands.group(
        name="ticket",
        invoke_without_command=True,
        help="Create and manage support tickets",
    )
    async def ticket(self, ctx: commands.Context):
        embed = discord.Embed(title="Tickets", color=COLOR)
        embed.description = (
            "`-ticket setup` / `-setup tickets` - Create or configure the Tickets category\n"
            "`-ticket config` - View ticket settings\n"
            "`-ticket osrole [@role]` - Set or create an OS role with full ticket access\n"
            "`-ticket managerrole @role` - Set the Ticket Manager role\n"
            "`-ticket staffrole @role` - Set the Ticket Staff role\n"
            "`-ticket logchannel #channel` - Set the private ticket log channel\n"
            "`-ticket panel #channel` - Send the Create a ticket panel\n"
            "`-ticket open` - Create your own ticket\n"
            "`-ticket claim` - Claim the current ticket\n"
            "`-ticket close` - Close and delete the current ticket\n"
            "`-ticket reopen` - Reopen the current ticket\n"
            "`-ticket info` - View ticket information\n"
            "`-ticket transcript` - Download the ticket message transcript"
        )
        await ctx.send(embed=embed)

    @ticket.command(name="setup", help="Create or configure the Tickets category")
    @commands.has_permissions(administrator=True)
    async def ticket_setup(self, ctx: commands.Context):
        await self.run_setup(ctx)

    @ticket.command(name="config", help="View ticket settings")
    @commands.has_permissions(manage_guild=True)
    async def ticket_config(self, ctx: commands.Context):
        cfg = self.store.guild(ctx.guild.id)

        def fmt(obj):
            return obj.mention if obj else "Not set"

        category = ctx.guild.get_channel(cfg["category_id"]) if cfg.get("category_id") else None
        log = ctx.guild.get_channel(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
        os_r = ctx.guild.get_role(cfg["os_role_id"]) if cfg.get("os_role_id") else None
        mgr = ctx.guild.get_role(cfg["manager_role_id"]) if cfg.get("manager_role_id") else None
        stf = ctx.guild.get_role(cfg["staff_role_id"]) if cfg.get("staff_role_id") else None

        embed = discord.Embed(title="Ticket Settings", color=COLOR)
        embed.add_field(name="Category", value=category.name if category else "Not set")
        embed.add_field(name="Log channel", value=fmt(log))
        embed.add_field(name="OS role", value=fmt(os_r))
        embed.add_field(name="Manager role", value=fmt(mgr))
        embed.add_field(name="Staff role", value=fmt(stf))
        embed.add_field(name="Open tickets", value=str(self.store.count_open(ctx.guild.id)))
        embed.set_footer(text=f"Total tickets created: {cfg['counter']}")
        await ctx.send(embed=embed)

    @ticket.command(name="osrole", help="Set or create an OS role with full ticket access")
    @commands.has_permissions(manage_guild=True)
    async def ticket_osrole(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        created = False
        if role is None:
            role = discord.utils.get(ctx.guild.roles, name="OS")
            if role is None:
                role = await ctx.guild.create_role(
                    name="OS", reason=f"Ticket OS role created by {ctx.author}"
                )
                created = True
        cfg = self.store.guild(ctx.guild.id)
        cfg["os_role_id"] = role.id
        self.store.save()
        await self.refresh_category(ctx.guild)
        await ctx.send(
            embed=discord.Embed(
                description=f"{'Created and set' if created else 'Set'} {role.mention} "
                "as the OS role with full ticket access.",
                color=discord.Color.green(),
            )
        )

    @ticket.command(name="managerrole", help="Set the Ticket Manager role")
    @commands.has_permissions(manage_guild=True)
    async def ticket_managerrole(self, ctx: commands.Context, role: discord.Role):
        cfg = self.store.guild(ctx.guild.id)
        cfg["manager_role_id"] = role.id
        self.store.save()
        await self.refresh_category(ctx.guild)
        await ctx.send(
            embed=discord.Embed(
                description=f"Ticket Manager role set to {role.mention}.",
                color=discord.Color.green(),
            )
        )

    @ticket.command(name="staffrole", help="Set the Ticket Staff role")
    @commands.has_permissions(manage_guild=True)
    async def ticket_staffrole(self, ctx: commands.Context, role: discord.Role):
        cfg = self.store.guild(ctx.guild.id)
        cfg["staff_role_id"] = role.id
        self.store.save()
        await self.refresh_category(ctx.guild)
        await ctx.send(
            embed=discord.Embed(
                description=f"Ticket Staff role set to {role.mention}.",
                color=discord.Color.green(),
            )
        )

    @ticket.command(name="logchannel", help="Set the private ticket log channel")
    @commands.has_permissions(manage_guild=True)
    async def ticket_logchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        cfg = self.store.guild(ctx.guild.id)
        cfg["log_channel_id"] = channel.id
        self.store.save()
        text = f"Ticket log channel set to {channel.mention}."
        if channel.permissions_for(ctx.guild.default_role).view_channel:
            text += "\n⚠️ Everyone can see that channel. Make it private so transcripts stay private."
        await ctx.send(embed=discord.Embed(description=text, color=discord.Color.green()))

    @ticket.command(name="panel", help="Send the Create a ticket panel")
    @commands.has_permissions(manage_guild=True)
    async def ticket_panel(self, ctx: commands.Context, channel: discord.TextChannel):
        cfg = self.store.guild(ctx.guild.id)
        if not cfg.get("category_id"):
            return await ctx.send("Run `-ticket setup` first.")
        embed = discord.Embed(
            title="Support Tickets",
            description="Need help? Click the button below to open a ticket "
            "and our staff will be with you shortly.",
            color=COLOR,
        )
        await channel.send(embed=embed, view=PanelView(self))
        await ctx.send(f"Ticket panel sent to {channel.mention}.")

    @ticket.command(name="open", help="Create your own ticket")
    async def ticket_open(self, ctx: commands.Context):
        channel, error = await self.create_ticket(ctx.guild, ctx.author)
        if error:
            return await ctx.send(error)
        await ctx.send(f"Your ticket has been created: {channel.mention}")

    @ticket.command(name="claim", help="Claim the current ticket")
    async def ticket_claim(self, ctx: commands.Context):
        ok, msg = await self.do_claim(ctx.channel, ctx.author)
        if not ok:
            await ctx.send(msg)

    @ticket.command(name="close", help="Close and delete the current ticket")
    async def ticket_close(self, ctx: commands.Context):
        error = await self.do_close(ctx.channel, ctx.author)
        if error:
            await ctx.send(error)

    @ticket.command(name="reopen", help="Reopen the current ticket")
    async def ticket_reopen(self, ctx: commands.Context):
        ok, msg = await self.do_reopen(ctx.channel, ctx.author)
        if not ok:
            await ctx.send(msg)

    @ticket.command(name="info", help="View ticket information")
    async def ticket_info(self, ctx: commands.Context):
        t = self.store.get_ticket(ctx.channel.id)
        if not t:
            return await ctx.send(NOT_TICKET)
        embed = discord.Embed(title=f"Ticket #{t['number']:04d}", color=COLOR)
        embed.add_field(name="Opened by", value=f"<@{t['owner_id']}>")
        embed.add_field(name="Created", value=f"<t:{t['created_at']}:F>")
        embed.add_field(name="Status", value=t["status"].title())
        embed.add_field(
            name="Claimed by",
            value=f"<@{t['claimed_by']}>" if t["claimed_by"] else "Nobody",
        )
        await ctx.send(embed=embed)

    @ticket.command(name="transcript", help="Download the ticket message transcript")
    async def ticket_transcript(self, ctx: commands.Context):
        t = self.store.get_ticket(ctx.channel.id)
        if not t:
            return await ctx.send(NOT_TICKET)
        cfg = self.store.guild(ctx.guild.id)
        if ctx.author.id != t["owner_id"] and not self.is_staff(ctx.author, cfg):
            return await ctx.send("You can't download this transcript.")
        async with ctx.typing():
            data = await self.transcript_bytes(ctx.channel)
        await ctx.send(
            "Here's the transcript:",
            file=discord.File(io.BytesIO(data), filename=f"transcript-{ctx.channel.name}.txt"),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
