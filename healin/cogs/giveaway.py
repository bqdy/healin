import json
import os
import random
import re
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

DATA_PATH = os.path.join("data", "giveaways.json")
MAX_WINNERS = 20
MIN_DURATION = 10  # seconds
MAX_DURATION = 60 * 60 * 24 * 60  # 60 days
NOT_FOUND = "I couldn't find a giveaway for that message. Use the message ID or link."

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.I)


def parse_duration(text: str) -> Optional[int]:
    text = text.strip().lower()
    matches = DURATION_RE.findall(text)
    if not matches or DURATION_RE.sub("", text).strip():
        return None
    return sum(int(n) * UNITS[u.lower()] for n, u in matches)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class GiveawayStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, message_id: int) -> Optional[dict]:
        return self.data.get(str(message_id))

    def put(self, g: dict):
        self.data[str(g["message_id"])] = g
        self.save()

    def active(self, guild_id: Optional[int] = None) -> list:
        return [
            g
            for g in self.data.values()
            if g["status"] == "active" and (guild_id is None or g["guild_id"] == guild_id)
        ]


# --------------------------------------------------------------------------- #
# Persistent entry button
# --------------------------------------------------------------------------- #
class EnterView(discord.ui.View):
    def __init__(self, cog: "Giveaway"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Enter",
        emoji="🎁",
        style=discord.ButtonStyle.primary,
        custom_id="giveaway:enter",
    )
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_enter(interaction)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Giveaway(commands.Cog, name="Giveaway"):
    """Voice channel giveaways"""

    HELP_LINES = [
        ("`{p}giveaway`", "View the giveaway guide"),
        (
            "`{p}giveaway start <duration> [winners=1] <prize>`",
            "Start a giveaway",
        ),
        ("`{p}giveaway end <message>`", "End and draw now"),
        ("`{p}giveaway cancel <message>`", "Cancel without drawing"),
        ("`{p}giveaway reroll <message> [winners]`", "Reroll an ended giveaway"),
        ("`{p}giveaway list`", "List active giveaways"),
        ("`{p}giveaway info <message>`", "View giveaway details"),
        ("`{p}giveaway entries <message>`", "View entries"),
    ]
    EDIT_LINES = [
        "`{p}giveaway edit prize <message> <prize>`",
        "`{p}giveaway edit duration <message> <duration>`",
        "`{p}giveaway edit winners <message> <amount>`",
        "`{p}giveaway edit host <message> @user`",
        "`{p}giveaway edit thumbnail <message> <URL/remove>`",
        "`{p}giveaway edit image <message> <URL/remove>`",
        "`{p}giveaway edit roles <message> <roles/none>`",
    ]

    # Picked up by the help menu in general.py ({p} becomes the bot prefix)
    help_text = (
        "# Giveaways\n\n"
        "**Management:** Requires Manage Channels. Press 🎁 to toggle entry; "
        "stay in VC. Leaving voice removes your entry.\n\n"
        + "\n".join(f"{u} - {d}" for u, d in HELP_LINES)
        + "\n\n## Editing\n\n"
        + "\n".join(EDIT_LINES)
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GiveawayStore(DATA_PATH)

    async def cog_load(self):
        self.bot.add_view(EnterView(self))
        self.check_giveaways.start()

    async def cog_unload(self):
        self.check_giveaways.cancel()

    async def cog_check(self, ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Channels** permission to manage giveaways.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument. Check the command usage with `-giveaway`.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        elif isinstance(error, discord.Forbidden):
            await ctx.send("I'm missing permissions to do that in this channel.")
        else:
            raise error

    # ----------------------------- helpers -------------------------------- #
    def resolve(self, ctx: commands.Context, arg: str) -> Optional[dict]:
        """Turn a message ID or message link into a giveaway record."""
        m = re.search(r"(\d{15,22})\D*$", arg.strip())
        if not m:
            return None
        g = self.store.get(int(m.group(1)))
        if g and g["guild_id"] == ctx.guild.id:
            return g
        return None

    @staticmethod
    def meets_roles(member: discord.Member, g: dict) -> bool:
        return not g["roles"] or any(r.id in g["roles"] for r in member.roles)

    @staticmethod
    async def ack(ctx: commands.Context):
        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

    def build_embed(self, g: dict) -> discord.Embed:
        status = g["status"]
        colors = {
            "active": discord.Color.green(),
            "ended": discord.Color.dark_grey(),
            "cancelled": discord.Color.red(),
        }
        lines = []
        if status == "active":
            lines.append(
                "Press 🎁 to toggle entry; stay in VC. Leaving voice removes your entry.\n"
            )
            lines.append(f"**Ends:** <t:{g['end_ts']}:R> (<t:{g['end_ts']}:f>)")
        elif status == "ended":
            lines.append(f"**Ended:** <t:{g.get('ended_ts', g['end_ts'])}:R>")
        else:
            lines.append("**Cancelled**")
        lines.append(f"**Hosted by:** <@{g['host_id']}>")
        lines.append(f"**Winners:** {g['winners']}")
        lines.append(f"**Entries:** {len(g['entries'])}")
        if g["roles"]:
            lines.append("**Required roles:** " + ", ".join(f"<@&{r}>" for r in g["roles"]))
        if status == "ended":
            mentions = ", ".join(f"<@{u}>" for u in g["winner_ids"])
            lines.append(f"**Winner(s):** {mentions or 'No valid entries'}")

        embed = discord.Embed(
            title=f"🎁 {g['prize']}",
            description="\n".join(lines),
            color=colors[status],
        )
        if g.get("thumbnail"):
            embed.set_thumbnail(url=g["thumbnail"])
        if g.get("image"):
            embed.set_image(url=g["image"])
        return embed

    async def refresh(self, g: dict):
        channel = self.bot.get_channel(g["channel_id"])
        if channel is None:
            return
        kwargs = {"embed": self.build_embed(g)}
        if g["status"] != "active":
            kwargs["view"] = None  # remove the button
        try:
            await channel.get_partial_message(g["message_id"]).edit(**kwargs)
        except discord.HTTPException:
            pass

    def pick_winners(self, guild: discord.Guild, g: dict, count: int, exclude=()) -> list:
        """Only entries still in a voice channel (and with the roles) are valid."""
        valid = []
        for uid in g["entries"]:
            if uid in exclude:
                continue
            m = guild.get_member(uid)
            if m and m.voice and m.voice.channel and self.meets_roles(m, g):
                valid.append(uid)
        return random.sample(valid, min(count, len(valid)))

    async def announce(self, g: dict, winners: list, reroll: bool = False):
        channel = self.bot.get_channel(g["channel_id"])
        if channel is None:
            return
        if winners:
            mentions = ", ".join(f"<@{u}>" for u in winners)
            if reroll:
                text = f"🎉 New winner(s): {mentions}! You won **{g['prize']}**!"
            else:
                text = f"🎉 Congratulations {mentions}! You won **{g['prize']}**!"
        else:
            text = f"No valid entries for **{g['prize']}**, so there are no winners."
        try:
            await channel.get_partial_message(g["message_id"]).reply(text)
        except discord.HTTPException:
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    async def finish(self, g: dict):
        guild = self.bot.get_guild(g["guild_id"])
        g["status"] = "ended"
        g["ended_ts"] = int(time.time())
        g["winner_ids"] = self.pick_winners(guild, g, g["winners"]) if guild else []
        self.store.save()
        await self.refresh(g)
        await self.announce(g, g["winner_ids"])

    # ------------------------- button + events ---------------------------- #
    async def handle_enter(self, interaction: discord.Interaction):
        g = self.store.get(interaction.message.id)
        if not g or g["status"] != "active":
            return await interaction.response.send_message(
                "This giveaway has ended.", ephemeral=True
            )
        member = interaction.user
        if not (member.voice and member.voice.channel):
            return await interaction.response.send_message(
                "You need to be in a voice channel to enter. "
                "Leaving voice removes your entry.",
                ephemeral=True,
            )
        if not self.meets_roles(member, g):
            roles = ", ".join(f"<@&{r}>" for r in g["roles"])
            return await interaction.response.send_message(
                f"You need one of these roles to enter: {roles}", ephemeral=True
            )

        if member.id in g["entries"]:
            g["entries"].remove(member.id)
            msg = "You left the giveaway."
        else:
            g["entries"].append(member.id)
            msg = "You're entered! Stay in voice to keep your entry."
        self.store.save()
        await interaction.response.send_message(msg, ephemeral=True)
        await self.refresh(g)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Leaving voice entirely removes the member's entries
        if before.channel is None or after.channel is not None:
            return
        changed = []
        for g in self.store.active(member.guild.id):
            if member.id in g["entries"]:
                g["entries"].remove(member.id)
                changed.append(g)
        if changed:
            self.store.save()
            for g in changed:
                await self.refresh(g)

    @tasks.loop(seconds=5)
    async def check_giveaways(self):
        now = int(time.time())
        for g in self.store.active():
            if g["end_ts"] <= now:
                try:
                    await self.finish(g)
                except Exception as e:  # keep the loop alive
                    print(f"[giveaway] failed to end {g['message_id']}: {e}")

    @check_giveaways.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------ commands ------------------------------ #
    @commands.group(
        name="giveaway",
        aliases=["gw"],
        invoke_without_command=True,
        help="View the giveaway guide",
    )
    async def giveaway(self, ctx: commands.Context):
        p = ctx.prefix
        embed = discord.Embed(title="Giveaway Guide", color=discord.Color.blurple())
        embed.description = (
            "**Management:** Requires Manage Channels.\n"
            "Press 🎁 to toggle entry; stay in VC. Leaving voice removes your entry.\n\n"
            f"**Start one:** `{p}giveaway start 1h winners=2 Nitro`\n"
            "**Durations:** `30s`, `10m`, `2h`, `1d`, `1w` or combined like `1h30m`.\n"
            "**`<message>`** is the giveaway's message ID or message link.\n\n"
            f"Use `{p}help` and pick **Giveaway** to see every command."
        )
        await ctx.send(embed=embed)

    @giveaway.command(name="start", help="Start a giveaway")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_start(self, ctx: commands.Context, duration: str, *, details: str):
        seconds = parse_duration(duration)
        if seconds is None or not (MIN_DURATION <= seconds <= MAX_DURATION):
            return await ctx.send(
                "Invalid duration. Use something like `30m`, `2h`, `1d` "
                "(10 seconds to 60 days)."
            )
        winners = 1
        prize = details.strip()
        m = re.match(r"(?i)^winners\s*=\s*(\d+)\s+(.+)$", prize, re.S)
        if m:
            winners = int(m.group(1))
            prize = m.group(2).strip()
        if not 1 <= winners <= MAX_WINNERS:
            return await ctx.send(f"Winners must be between 1 and {MAX_WINNERS}.")
        if len(prize) > 200:
            return await ctx.send("The prize is too long (max 200 characters).")

        g = {
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
            "message_id": None,
            "host_id": ctx.author.id,
            "prize": prize,
            "winners": winners,
            "end_ts": int(time.time()) + seconds,
            "status": "active",
            "entries": [],
            "winner_ids": [],
            "roles": [],
            "thumbnail": None,
            "image": None,
        }
        msg = await ctx.send(embed=self.build_embed(g), view=EnterView(self))
        g["message_id"] = msg.id
        self.store.put(g)

    @giveaway.command(name="end", help="End and draw now")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_end(self, ctx: commands.Context, *, message: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if g["status"] != "active":
            return await ctx.send("That giveaway isn't active.")
        await self.finish(g)
        await self.ack(ctx)

    @giveaway.command(name="cancel", help="Cancel without drawing")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_cancel(self, ctx: commands.Context, *, message: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if g["status"] != "active":
            return await ctx.send("That giveaway isn't active.")
        g["status"] = "cancelled"
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)

    @giveaway.command(name="reroll", help="Reroll an ended giveaway")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_reroll(
        self, ctx: commands.Context, message: str, winners: Optional[int] = None
    ):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if g["status"] != "ended":
            return await ctx.send("Only ended giveaways can be rerolled.")
        count = winners or g["winners"]
        if not 1 <= count <= MAX_WINNERS:
            return await ctx.send(f"Winners must be between 1 and {MAX_WINNERS}.")
        picks = self.pick_winners(ctx.guild, g, count, exclude=set(g["winner_ids"]))
        if not picks:
            return await ctx.send("There are no other valid entries to reroll.")
        g["winner_ids"] = picks
        self.store.save()
        await self.refresh(g)
        await self.announce(g, picks, reroll=True)

    @giveaway.command(name="list", help="List active giveaways")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_list(self, ctx: commands.Context):
        active = self.store.active(ctx.guild.id)
        if not active:
            return await ctx.send("There are no active giveaways.")
        lines = []
        for g in sorted(active, key=lambda x: x["end_ts"]):
            url = (
                f"https://discord.com/channels/{g['guild_id']}/"
                f"{g['channel_id']}/{g['message_id']}"
            )
            lines.append(
                f"**[{g['prize']}]({url})** - <#{g['channel_id']}> - "
                f"ends <t:{g['end_ts']}:R> - {len(g['entries'])} entries"
            )
        embed = discord.Embed(
            title="Active Giveaways",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @giveaway.command(name="info", help="View giveaway details")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_info(self, ctx: commands.Context, *, message: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        url = (
            f"https://discord.com/channels/{g['guild_id']}/"
            f"{g['channel_id']}/{g['message_id']}"
        )
        embed = discord.Embed(
            title=f"🎁 {g['prize']}", url=url, color=discord.Color.blurple()
        )
        embed.add_field(name="Status", value=g["status"].title())
        embed.add_field(name="Host", value=f"<@{g['host_id']}>")
        embed.add_field(name="Winners", value=str(g["winners"]))
        embed.add_field(name="Entries", value=str(len(g["entries"])))
        embed.add_field(name="Channel", value=f"<#{g['channel_id']}>")
        embed.add_field(
            name="Ends" if g["status"] == "active" else "Ended",
            value=f"<t:{g['end_ts'] if g['status'] == 'active' else g.get('ended_ts', g['end_ts'])}:f>",
        )
        embed.add_field(
            name="Required roles",
            value=", ".join(f"<@&{r}>" for r in g["roles"]) or "None",
            inline=False,
        )
        if g["winner_ids"]:
            embed.add_field(
                name="Winner(s)",
                value=", ".join(f"<@{u}>" for u in g["winner_ids"]),
                inline=False,
            )
        await ctx.send(embed=embed)

    @giveaway.command(name="entries", help="View entries")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_entries(self, ctx: commands.Context, *, message: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if not g["entries"]:
            return await ctx.send("There are no entries yet.")
        lines, size = [], 0
        for i, uid in enumerate(g["entries"], 1):
            line = f"{i}. <@{uid}>"
            if size + len(line) > 3800:
                lines.append(f"...and {len(g['entries']) - i + 1} more")
                break
            lines.append(line)
            size += len(line) + 1
        embed = discord.Embed(
            title=f"Entries ({len(g['entries'])})",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    # ------------------------------- editing ------------------------------ #
    @giveaway.group(name="edit", invoke_without_command=True, help="Edit a giveaway")
    @commands.has_permissions(manage_channels=True)
    async def giveaway_edit(self, ctx: commands.Context):
        p = ctx.prefix
        lines = "\n".join(l.replace("{p}", p) for l in self.EDIT_LINES)
        await ctx.send(embed=discord.Embed(title="Editing", description=lines, color=discord.Color.blurple()))

    @giveaway_edit.command(name="prize", help="Change the prize")
    @commands.has_permissions(manage_channels=True)
    async def edit_prize(self, ctx: commands.Context, message: str, *, prize: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if len(prize) > 200:
            return await ctx.send("The prize is too long (max 200 characters).")
        g["prize"] = prize.strip()
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)

    @giveaway_edit.command(name="duration", help="Set the time left from now")
    @commands.has_permissions(manage_channels=True)
    async def edit_duration(self, ctx: commands.Context, message: str, duration: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if g["status"] != "active":
            return await ctx.send("That giveaway isn't active.")
        seconds = parse_duration(duration)
        if seconds is None or not (MIN_DURATION <= seconds <= MAX_DURATION):
            return await ctx.send("Invalid duration. Use something like `30m`, `2h`, `1d`.")
        g["end_ts"] = int(time.time()) + seconds
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)

    @giveaway_edit.command(name="winners", help="Change the number of winners")
    @commands.has_permissions(manage_channels=True)
    async def edit_winners(self, ctx: commands.Context, message: str, amount: int):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if g["status"] != "active":
            return await ctx.send("That giveaway isn't active. Use `reroll` instead.")
        if not 1 <= amount <= MAX_WINNERS:
            return await ctx.send(f"Winners must be between 1 and {MAX_WINNERS}.")
        g["winners"] = amount
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)

    @giveaway_edit.command(name="host", help="Change the host")
    @commands.has_permissions(manage_channels=True)
    async def edit_host(self, ctx: commands.Context, message: str, user: discord.Member):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        g["host_id"] = user.id
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)

    async def _set_media(self, ctx, message: str, url: str, key: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        url = url.strip().strip("<>")
        if url.lower() == "remove":
            g[key] = None
        elif url.lower().startswith(("http://", "https://")):
            g[key] = url
        else:
            return await ctx.send("Provide a valid image URL (https://...) or `remove`.")
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)

    @giveaway_edit.command(name="thumbnail", help="Set or remove the thumbnail")
    @commands.has_permissions(manage_channels=True)
    async def edit_thumbnail(self, ctx: commands.Context, message: str, url: str):
        await self._set_media(ctx, message, url, "thumbnail")

    @giveaway_edit.command(name="image", help="Set or remove the image")
    @commands.has_permissions(manage_channels=True)
    async def edit_image(self, ctx: commands.Context, message: str, url: str):
        await self._set_media(ctx, message, url, "image")

    @giveaway_edit.command(name="roles", help="Set the required roles (or none)")
    @commands.has_permissions(manage_channels=True)
    async def edit_roles(self, ctx: commands.Context, message: str, *, roles: str):
        g = self.resolve(ctx, message)
        if not g:
            return await ctx.send(NOT_FOUND)
        if g["status"] != "active":
            return await ctx.send("That giveaway isn't active.")
        if roles.strip().lower() == "none":
            g["roles"] = []
        else:
            converter = commands.RoleConverter()
            found = []
            for token in roles.split():
                try:
                    found.append((await converter.convert(ctx, token)).id)
                except commands.BadArgument:
                    return await ctx.send(
                        f"I couldn't find the role `{token}`. Mention roles or use IDs, "
                        "or type `none`."
                    )
            g["roles"] = found
        self.store.save()
        await self.refresh(g)
        await self.ack(ctx)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
