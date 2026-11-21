import asyncio
import json
import os
import time
from typing import Optional

import discord
from discord.ext import commands

DATA_PATH = os.path.join("data", "disconnect_security.json")
COLOR = discord.Color.blurple()
DEFAULTS = {
    "enabled": False,
    "threshold": 3,
    "window_minutes": 5,
    "log_channel_id": None,
    "ping_role_id": None,
}


def ok(text: str) -> discord.Embed:
    return discord.Embed(description=text, color=discord.Color.green())


class DisconnectSecurity(commands.Cog, name="Disconnect Security"):
    """Stops members from mass-disconnecting people from voice"""

    MAIN_LINES = [
        ("`{p}disconnectsecurity`", "View disconnect-security help"),
        ("`{p}disconnectsecurity setup`", "Set up disconnect security"),
        ("`{p}disconnectsecurity status`", "View current settings"),
        (
            "`{p}disconnectsecurity threshold <count> [window_minutes]`",
            "Set the trigger limit",
        ),
        ("`{p}disconnectsecurity logchannel #channel`", "Set the log channel"),
        ("`{p}disconnectsecurity pingrole @role`", "Set or toggle the alert role"),
        ("`{p}disconnectsecurity reset @user`", "Clear a member's disconnect history"),
        ("`{p}disconnectsecurity check @user`", "Check a member's disconnect history"),
    ]
    SHORT_LINES = [
        ("`{p}disconnect threshold set <count> [window]`", "Set the trigger limit"),
        ("`{p}disconnect threshold view`", "View the trigger limit"),
        ("`{p}disconnect reset @user`", "Clear a member's disconnect history"),
    ]

    HELP_BODY = (
        "At the disconnect threshold, the bot removes the offender's manageable roles "
        "granting Discord Move Members or Administrator. Roles above the bot and "
        "managed roles cannot be removed.\n\n"
        + "\n".join(f"{u} - {d}" for u, d in MAIN_LINES)
        + "\n\n## Short Commands\n\n"
        + "\n".join(f"{u} - {d}" for u, d in SHORT_LINES)
    )

    # Picked up by the help menu in general.py ({p} becomes the bot prefix)
    help_text = "# Disconnect Security\n\n" + HELP_BODY

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: dict = {}
        self.history: dict[tuple[int, int], list[float]] = {}  # (guild, user) -> timestamps
        self.seen: dict[int, int] = {}  # audit entry id -> disconnect count already counted
        self.seeded: set[int] = set()
        self._running: set[int] = set()
        self._dirty: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self.load()

    # ------------------------------ storage ------------------------------- #
    def load(self):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def save(self):
        os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
        tmp = DATA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, DATA_PATH)

    def cfg(self, guild_id: int) -> dict:
        c = self.data.setdefault(str(guild_id), {})
        for k, v in DEFAULTS.items():
            c.setdefault(k, v)
        return c

    # ------------------------------ lifecycle ----------------------------- #
    async def cog_load(self):
        self._spawn(self._seed_all())

    async def cog_check(self, ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Administrator** permission to use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument. Check the usage with `-disconnectsecurity`.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        else:
            raise error

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----------------------------- detection ------------------------------ #
    async def _seed_all(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            if self.cfg(guild.id)["enabled"]:
                await self.seed(guild)

    async def seed(self, guild: discord.Guild):
        """Remember existing audit entries so old disconnects aren't counted."""
        try:
            async for entry in guild.audit_logs(
                limit=25, action=discord.AuditLogAction.member_disconnect
            ):
                self.seen[entry.id] = getattr(entry.extra, "count", 1) or 1
            self.seeded.add(guild.id)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Somebody left voice. If it was a forced disconnect, the audit log shows who did it.
        if before.channel is None or after.channel is not None:
            return
        if not self.cfg(member.guild.id)["enabled"]:
            return
        self._spawn(self._check_guild(member.guild))

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if entry.action is discord.AuditLogAction.member_disconnect:
            await self.process_entry(entry.guild, entry)

    async def _check_guild(self, guild: discord.Guild):
        if guild.id in self._running:
            self._dirty.add(guild.id)
            return
        self._running.add(guild.id)
        try:
            while True:
                self._dirty.discard(guild.id)
                await asyncio.sleep(1.5)  # give Discord time to write the audit entry
                await self.scan(guild)
                if guild.id not in self._dirty:
                    break
        finally:
            self._running.discard(guild.id)

    async def scan(self, guild: discord.Guild):
        if guild.id not in self.seeded:
            await self.seed(guild)
            return
        try:
            async for entry in guild.audit_logs(
                limit=10, action=discord.AuditLogAction.member_disconnect
            ):
                await self.process_entry(guild, entry)
        except (discord.Forbidden, discord.HTTPException):
            pass  # missing View Audit Log; `status` warns about this

    async def process_entry(self, guild: discord.Guild, entry: discord.AuditLogEntry):
        cfg = self.cfg(guild.id)
        if not cfg["enabled"]:
            return

        count = getattr(entry.extra, "count", 1) or 1
        already = self.seen.get(entry.id, 0)
        delta = count - already
        self.seen[entry.id] = max(already, count)
        if len(self.seen) > 2000:
            self.seen = dict(sorted(self.seen.items())[-1000:])
        if delta <= 0:
            return

        executor = entry.user
        if executor is None or executor.id == self.bot.user.id or executor.id == guild.owner_id:
            return

        now = time.time()
        window = cfg["window_minutes"] * 60
        key = (guild.id, executor.id)
        hist = [t for t in self.history.get(key, []) if now - t <= window]
        hist.extend([now] * delta)
        self.history[key] = hist

        if len(hist) >= cfg["threshold"]:
            self.history.pop(key, None)
            await self.trigger(guild, executor, len(hist), cfg)

    async def trigger(self, guild: discord.Guild, user, count: int, cfg: dict):
        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except discord.HTTPException:
                member = None

        removed, failed = [], []
        if member:
            me = guild.me
            for role in member.roles:
                if role.is_default():
                    continue
                perms = role.permissions
                if not (perms.move_members or perms.administrator):
                    continue
                if role.managed or role.position >= me.top_role.position:
                    failed.append(role)
                else:
                    removed.append(role)
            if removed:
                try:
                    await member.remove_roles(
                        *removed,
                        reason=f"Disconnect Security: {count} disconnects in "
                        f"{cfg['window_minutes']} minute(s)",
                    )
                except discord.HTTPException:
                    failed.extend(removed)
                    removed = []

        embed = discord.Embed(title="Disconnect Security Triggered", color=discord.Color.red())
        embed.add_field(name="Offender", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(
            name="Disconnects",
            value=f"{count} within {cfg['window_minutes']} minute(s) (limit {cfg['threshold']})",
            inline=False,
        )
        embed.add_field(
            name="Roles removed",
            value=", ".join(r.mention for r in removed) or "None",
            inline=False,
        )
        if failed:
            embed.add_field(
                name="Couldn't remove (above the bot or managed)",
                value=", ".join(r.mention for r in failed),
                inline=False,
            )
        if member is None:
            embed.set_footer(text="The offender is no longer in the server.")

        channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        if isinstance(channel, discord.TextChannel):
            ping = f"<@&{cfg['ping_role_id']}>" if cfg["ping_role_id"] else None
            try:
                await channel.send(
                    content=ping,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except discord.HTTPException:
                pass

    # ------------------------------ shared logic -------------------------- #
    async def send_guide(self, ctx: commands.Context):
        embed = discord.Embed(
            title="Disconnect Security",
            description=self.HELP_BODY.replace("{p}", ctx.prefix),
            color=COLOR,
        )
        await ctx.send(embed=embed)

    async def do_setup(self, ctx: commands.Context):
        cfg = self.cfg(ctx.guild.id)
        cfg["enabled"] = True
        self.save()
        await self.seed(ctx.guild)

        perms = ctx.guild.me.guild_permissions
        warnings = []
        if not perms.view_audit_log:
            warnings.append("• I need **View Audit Log** to see who disconnects people.")
        if not perms.manage_roles:
            warnings.append("• I need **Manage Roles** to remove dangerous roles.")

        embed = discord.Embed(
            title="Disconnect Security Enabled",
            description=(
                f"Trigger: **{cfg['threshold']}** disconnects within "
                f"**{cfg['window_minutes']}** minute(s).\n\n"
                f"Next: `{ctx.prefix}disconnectsecurity logchannel #channel` and "
                f"`{ctx.prefix}disconnectsecurity pingrole @role`."
            ),
            color=discord.Color.green(),
        )
        if warnings:
            embed.add_field(name="⚠️ Missing permissions", value="\n".join(warnings))
        await ctx.send(embed=embed)

    async def do_status(self, ctx: commands.Context):
        cfg = self.cfg(ctx.guild.id)
        log = ctx.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        role = ctx.guild.get_role(cfg["ping_role_id"]) if cfg["ping_role_id"] else None
        perms = ctx.guild.me.guild_permissions

        embed = discord.Embed(title="Disconnect Security", color=COLOR)
        embed.add_field(name="Enabled", value="Yes" if cfg["enabled"] else "No")
        embed.add_field(
            name="Trigger limit",
            value=f"{cfg['threshold']} disconnects / {cfg['window_minutes']} min",
        )
        embed.add_field(name="Log channel", value=log.mention if log else "Not set")
        embed.add_field(name="Alert role", value=role.mention if role else "Not set")
        embed.add_field(
            name="Bot permissions",
            value=(
                f"View Audit Log: {'✅' if perms.view_audit_log else '❌'}\n"
                f"Manage Roles: {'✅' if perms.manage_roles else '❌'}"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    async def do_threshold(self, ctx: commands.Context, count: int, window: Optional[int]):
        if not 1 <= count <= 50:
            return await ctx.send("Count must be between 1 and 50.")
        if window is not None and not 1 <= window <= 60:
            return await ctx.send("The window must be between 1 and 60 minutes.")
        cfg = self.cfg(ctx.guild.id)
        cfg["threshold"] = count
        if window is not None:
            cfg["window_minutes"] = window
        self.save()
        await ctx.send(
            embed=ok(
                f"Trigger limit set to **{cfg['threshold']}** disconnects within "
                f"**{cfg['window_minutes']}** minute(s)."
            )
        )

    async def do_threshold_view(self, ctx: commands.Context):
        cfg = self.cfg(ctx.guild.id)
        await ctx.send(
            embed=discord.Embed(
                description=(
                    f"Trigger limit: **{cfg['threshold']}** disconnects within "
                    f"**{cfg['window_minutes']}** minute(s)."
                ),
                color=COLOR,
            )
        )

    async def do_reset(self, ctx: commands.Context, user: discord.User):
        self.history.pop((ctx.guild.id, user.id), None)
        await ctx.send(embed=ok(f"Cleared {user.mention}'s disconnect history."))

    # ------------------------------ commands ------------------------------ #
    @commands.group(
        name="disconnectsecurity",
        invoke_without_command=True,
        help="View disconnect-security help",
    )
    async def disconnectsecurity(self, ctx: commands.Context):
        await self.send_guide(ctx)

    @disconnectsecurity.command(
        name="setup", aliases=["setupds"], help="Set up disconnect security"
    )
    @commands.has_permissions(administrator=True)
    async def ds_setup(self, ctx: commands.Context):
        await self.do_setup(ctx)

    @disconnectsecurity.command(name="status", help="View current settings")
    @commands.has_permissions(administrator=True)
    async def ds_status(self, ctx: commands.Context):
        await self.do_status(ctx)

    @disconnectsecurity.command(name="threshold", help="Set the trigger limit")
    @commands.has_permissions(administrator=True)
    async def ds_threshold(
        self, ctx: commands.Context, count: int, window_minutes: Optional[int] = None
    ):
        await self.do_threshold(ctx, count, window_minutes)

    @disconnectsecurity.command(name="logchannel", help="Set the log channel")
    @commands.has_permissions(administrator=True)
    async def ds_logchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        cfg = self.cfg(ctx.guild.id)
        cfg["log_channel_id"] = channel.id
        self.save()
        text = f"Log channel set to {channel.mention}."
        if not channel.permissions_for(ctx.guild.me).send_messages:
            text += "\n⚠️ I can't send messages in that channel."
        await ctx.send(embed=ok(text))

    @disconnectsecurity.command(name="pingrole", help="Set or toggle the alert role")
    @commands.has_permissions(administrator=True)
    async def ds_pingrole(self, ctx: commands.Context, role: discord.Role):
        cfg = self.cfg(ctx.guild.id)
        if cfg["ping_role_id"] == role.id:
            cfg["ping_role_id"] = None
            text = f"{role.mention} will no longer be pinged on alerts."
        else:
            cfg["ping_role_id"] = role.id
            text = f"{role.mention} will be pinged on alerts."
        self.save()
        await ctx.send(embed=ok(text))

    @disconnectsecurity.command(name="reset", help="Clear a member's disconnect history")
    @commands.has_permissions(administrator=True)
    async def ds_reset(self, ctx: commands.Context, user: discord.User):
        await self.do_reset(ctx, user)

    @disconnectsecurity.command(name="check", help="Check a member's disconnect history")
    @commands.has_permissions(administrator=True)
    async def ds_check(self, ctx: commands.Context, user: discord.User):
        cfg = self.cfg(ctx.guild.id)
        now = time.time()
        window = cfg["window_minutes"] * 60
        hist = [t for t in self.history.get((ctx.guild.id, user.id), []) if now - t <= window]
        text = (
            f"{user.mention} has **{len(hist)}** disconnect(s) in the last "
            f"**{cfg['window_minutes']}** minute(s). Limit: **{cfg['threshold']}**."
        )
        if hist:
            text += f"\nMost recent: <t:{int(max(hist))}:R>"
        await ctx.send(embed=discord.Embed(description=text, color=COLOR))

    # ---- short commands: -disconnect ... ---- #
    @commands.group(
        name="disconnect",
        invoke_without_command=True,
        help="View disconnect-security help",
    )
    async def disconnect(self, ctx: commands.Context):
        await self.send_guide(ctx)

    @disconnect.group(
        name="threshold", invoke_without_command=True, help="View the trigger limit"
    )
    @commands.has_permissions(administrator=True)
    async def disconnect_threshold(self, ctx: commands.Context):
        await self.do_threshold_view(ctx)

    @disconnect_threshold.command(name="set", help="Set the trigger limit")
    @commands.has_permissions(administrator=True)
    async def disconnect_threshold_set(
        self, ctx: commands.Context, count: int, window: Optional[int] = None
    ):
        await self.do_threshold(ctx, count, window)

    @disconnect_threshold.command(name="view", help="View the trigger limit")
    @commands.has_permissions(administrator=True)
    async def disconnect_threshold_view(self, ctx: commands.Context):
        await self.do_threshold_view(ctx)

    @disconnect.command(name="reset", help="Clear a member's disconnect history")
    @commands.has_permissions(administrator=True)
    async def disconnect_reset(self, ctx: commands.Context, user: discord.User):
        await self.do_reset(ctx, user)


async def setup(bot: commands.Bot):
    await bot.add_cog(DisconnectSecurity(bot))
