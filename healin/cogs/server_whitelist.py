"""Server whitelist: the bot only stays in servers that are whitelisted.

  swl <server id> <time>    whitelist a server for a while (7 days, 12h, 1 year, yearly ...)
  swl remove <server id>    remove the whitelist and leave that server if the bot is in it
  swl list                  show whitelisted servers
  uwl add|remove|list       who may use swl (add/remove are bot owner only)

If the bot joins a server that isn't whitelisted it leaves straight away, and it leaves when a
whitelist runs out. The bot owner always has access. Data lives in data/server_whitelist.json.

Every time the bot starts, any server it's already in that isn't on the list gets a lifetime
whitelist (no expiry), so turning this on never kicks it out of servers it's already in. Use
`swl remove <id>` to drop one. Servers it joins while running must be whitelisted first.
"""
import os
import re
import time
import traceback
from typing import Optional

import discord
from discord.ext import commands, tasks

from utils.paginator import chunk_pages, paginate
from utils.store import GuildStore

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "482506940027502597"))
COLOR = 0x0F0F10
NO_PINGS = discord.AllowedMentions.none()
KEY = "shared"  # one list for the whole bot
SWEEP_SECONDS = 10  # how often expired whitelists are checked

DAY = 86400
YEAR = 365 * DAY
MAX_SECONDS = 100 * YEAR
LIFETIME_WORDS = ("lifetime", "forever", "permanent")
UNITS = {}
for _names, _secs in [
    (("s", "sec", "secs", "second", "seconds"), 1),
    (("m", "min", "mins", "minute", "minutes"), 60),
    (("h", "hr", "hrs", "hour", "hours"), 3600),
    (("d", "day", "days"), DAY),
    (("mo", "mos", "month", "months"), 30 * DAY),
    (("y", "yr", "yrs", "year", "years"), YEAR),
]:
    for _n in _names:
        UNITS[_n] = _secs


def parse_duration(text: str) -> Optional[int]:
    """'7 days', '12h', '1 year', 'yearly' -> seconds (a month is 30 days, a year 365). None if unreadable."""
    t = text.strip().lower()
    if t in ("yearly", "annually", "annual"):
        return YEAR
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", t)
    if not m or m.group(2) not in UNITS:
        return None
    secs = int(float(m.group(1)) * UNITS[m.group(2)])
    return secs if 1 <= secs <= MAX_SECONDS else None


def bot_owner_only():
    async def predicate(ctx):
        if ctx.author.id != BOT_OWNER_ID:
            raise commands.NotOwner("Only the bot owner can use that.")
        return True
    return commands.check(predicate)


class ServerWhitelist(commands.Cog, name="Server Whitelist"):
    """Controls which servers the bot may stay in"""

    def __init__(self, bot):
        self.bot = bot
        self.store = GuildStore("server_whitelist", {"seeded": False, "servers": {}, "users": []})

    # ---- data helpers ------------------------------------------------------
    def data(self) -> dict:
        return self.store.get(KEY)

    def allowed(self, guild_id: int) -> bool:
        e = self.data()["servers"].get(str(guild_id))
        return bool(e) and (e["expires"] is None or e["expires"] > time.time())

    def has_access(self, user_id: int) -> bool:
        return user_id == BOT_OWNER_ID or user_id in self.data()["users"]

    def seed_existing(self) -> None:
        """On every start: give servers the bot is already in, but that aren't listed, a lifetime whitelist."""
        d = self.data()
        added = 0
        for g in self.bot.guilds:
            if str(g.id) not in d["servers"]:
                d["servers"][str(g.id)] = {"expires": None, "by": 0, "at": int(time.time())}
                added += 1
        d["seeded"] = True
        self.store.save()
        if added:
            print(f"[swl] gave {added} server(s) I'm already in a lifetime whitelist")

    async def leave(self, guild: discord.Guild, why: str) -> bool:
        try:
            await guild.leave()
        except discord.HTTPException as e:
            print(f"[swl] couldn't leave {guild.name} ({guild.id}): {e!r}")
            return False
        print(f"[swl] left {guild.name} ({guild.id}): {why}")
        return True

    async def sweep(self) -> None:
        """Drop expired whitelists, then leave every server that isn't whitelisted."""
        d = self.data()
        now = time.time()
        expired = [gid for gid, e in d["servers"].items() if e["expires"] is not None and e["expires"] <= now]
        for gid in expired:
            del d["servers"][gid]
        if expired:
            self.store.save()
        for guild in list(self.bot.guilds):
            if not self.allowed(guild.id):
                await self.leave(guild, "not whitelisted" if str(guild.id) not in expired else "whitelist expired")

    async def user_name(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                pass
        return discord.utils.escape_markdown(user.name) if user else f"Unknown user ({user_id})"

    # ---- lifecycle ------------------------------------------------------------
    async def cog_load(self):
        self.sweeper.start()

    async def cog_unload(self):
        self.sweeper.cancel()

    @tasks.loop(seconds=SWEEP_SECONDS)
    async def sweeper(self):
        try:
            await self.sweep()
        except Exception:
            traceback.print_exc()

    @sweeper.before_loop
    async def before_sweeper(self):
        await self.bot.wait_until_ready()
        self.seed_existing()

    async def inviter_of(self, guild: discord.Guild):
        """Who added the bot (from the audit log, if the bot can read it), else the server owner."""
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                if entry.target is not None and entry.target.id == self.bot.user.id and entry.user:
                    return entry.user
        except discord.HTTPException:  # no View Audit Log permission, or Discord had a hiccup
            pass
        if guild.owner is not None:
            return guild.owner
        try:
            return await self.bot.fetch_user(guild.owner_id)
        except discord.HTTPException:
            return None

    async def explain_rejection(self, guild: discord.Guild) -> None:
        """DM whoever added the bot to say why it's leaving. Never lets a failure stop the leave."""
        try:
            who = await self.inviter_of(guild)
            if who is None or who.bot:
                return
            owner = await self.user_name(BOT_OWNER_ID)
            await who.send(
                f"Hi! I left **{discord.utils.escape_markdown(guild.name)}** because that server isn't "
                f"whitelisted. I only work in servers that have been approved.\n"
                f"To use me there, ask **{owner}** (the bot owner) to whitelist the server ID `{guild.id}`, "
                f"then add me again."
            )
        except (discord.HTTPException, AttributeError):
            pass  # their DMs are closed, or we couldn't work out who added the bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if self.data()["seeded"] and not self.allowed(guild.id):
            await self.explain_rejection(guild)  # before leaving, so the audit log is still readable
            await self.leave(guild, "invited without being whitelisted")

    # ---- access + errors -------------------------------------------------------
    async def cog_check(self, ctx):
        if not self.has_access(ctx.author.id):
            raise commands.CheckFailure("not on the user whitelist")
        return True

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NotOwner):
            msg = "Only the bot owner can use that."
        elif isinstance(error, commands.CheckFailure):
            msg = "You're not on the user whitelist."
        elif isinstance(error, commands.UserInputError):
            msg = f"Usage: `{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`".strip()
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            msg = f"Something went wrong: `{error}`"
        await ctx.send(msg, allowed_mentions=NO_PINGS)

    # ---- swl -----------------------------------------------------------------
    @commands.group(name="swl", invoke_without_command=True)
    async def swl(self, ctx, server_id: Optional[int] = None, *, duration: Optional[str] = None):
        """Whitelist a server for a set time"""
        p = ctx.clean_prefix
        usage = (f"Usage: `{p}swl <server id> <time>`, e.g. `{p}swl 123 7 days`, `{p}swl 123 12h`, "
                 f"`{p}swl 123 1 year`, `{p}swl 123 yearly` or `{p}swl 123 lifetime`\n"
                 f"Also `{p}swl remove <server id>` and `{p}swl list`.")
        if server_id is None or not duration:
            return await ctx.send(usage)
        lifetime = duration.strip().lower() in LIFETIME_WORDS
        seconds = None if lifetime else parse_duration(duration)
        if server_id < 1 or (seconds is None and not lifetime):
            return await ctx.send("I couldn't read that. Time can be seconds, minutes, hours, days, months, "
                                  "years or `yearly`.\n" + usage)
        now = int(time.time())
        expires = None if lifetime else now + seconds
        self.data()["servers"][str(server_id)] = {"expires": expires, "by": ctx.author.id, "at": now}
        self.store.save()
        guild = self.bot.get_guild(server_id)
        name = f" (**{discord.utils.escape_markdown(guild.name)}**)" if guild else ""
        until = "for life (it never expires)" if lifetime else f"until <t:{expires}:F> (<t:{expires}:R>)"
        await ctx.send(f"✅ Server `{server_id}`{name} is whitelisted {until}.", allowed_mentions=NO_PINGS)

    @swl.command(name="remove")
    async def swl_remove(self, ctx, server_id: int):
        """Remove a server's whitelist and leave it"""
        existed = self.data()["servers"].pop(str(server_id), None) is not None
        self.store.save()
        guild = self.bot.get_guild(server_id)
        name = f"**{discord.utils.escape_markdown(guild.name)}** (`{server_id}`)" if guild else f"`{server_id}`"
        if existed and guild:
            msg = f"✅ Removed the whitelist for {name}. Leaving it now."
        elif existed:
            msg = f"✅ Removed the whitelist for {name}. I'm not in that server."
        elif guild:
            msg = f"{name} wasn't whitelisted, but I was in it. Leaving it now."
        else:
            msg = f"{name} isn't whitelisted and I'm not in it."
        await ctx.send(msg, allowed_mentions=NO_PINGS)  # sent first, in case this is the server being left
        if guild and not await self.leave(guild, f"whitelist removed by {ctx.author}"):
            await ctx.send("⚠️ I couldn't leave that server (check the console).")

    @swl.command(name="list")
    async def swl_list(self, ctx):
        """Show whitelisted servers"""
        now = time.time()
        rows = sorted(((gid, e) for gid, e in self.data()["servers"].items()
                       if e["expires"] is None or e["expires"] > now),
                      key=lambda r: (r[1]["expires"] is None, r[1]["expires"] or 0))
        lines = []
        for i, (gid, e) in enumerate(rows, 1):
            guild = self.bot.get_guild(int(gid))
            name = discord.utils.escape_markdown(guild.name) if guild else "Not joined"
            until = "never expires" if e["expires"] is None else f"until <t:{e['expires']}:f> (<t:{e['expires']}:R>)"
            lines.append(f"`{i}.` **{name}** (`{gid}`) — {until}")
        await paginate(ctx, chunk_pages("Whitelisted servers", lines, COLOR, per_page=10,
                                        empty="No servers are whitelisted."))

    # ---- uwl -----------------------------------------------------------------
    @commands.group(name="uwl", invoke_without_command=True)
    @bot_owner_only()
    async def uwl(self, ctx, user: Optional[discord.User] = None):
        """Give someone access to the swl commands (bot owner only)"""
        if user is None:
            p = ctx.clean_prefix
            return await ctx.send(f"Usage: `{p}uwl add <user>`, `{p}uwl remove <user>`, `{p}uwl list`")
        await self.uwl_add(ctx, user)

    @uwl.command(name="add")
    @bot_owner_only()
    async def uwl_add(self, ctx, user: discord.User):
        """Let someone use the swl commands"""
        users = self.data()["users"]
        if user.bot:
            return await ctx.send("Bots can't be added.")
        if user.id == BOT_OWNER_ID:
            return await ctx.send("The bot owner always has access.")
        if user.id in users:
            return await ctx.send(f"**{discord.utils.escape_markdown(user.name)}** is already on the user whitelist.",
                                  allowed_mentions=NO_PINGS)
        users.append(user.id)
        self.store.save()
        await ctx.send(f"✅ **{discord.utils.escape_markdown(user.name)}** can now use the swl commands.",
                       allowed_mentions=NO_PINGS)

    @uwl.command(name="remove")
    @bot_owner_only()
    async def uwl_remove(self, ctx, user: discord.User):
        """Take away someone's swl access"""
        users = self.data()["users"]
        name = discord.utils.escape_markdown(user.name)
        if user.id not in users:
            return await ctx.send(f"**{name}** isn't on the user whitelist.", allowed_mentions=NO_PINGS)
        users.remove(user.id)
        self.store.save()
        await ctx.send(f"✅ **{name}** can no longer use the swl commands.", allowed_mentions=NO_PINGS)

    @uwl.command(name="list")
    async def uwl_list(self, ctx):
        """Show who has swl access"""
        lines = [f"`{i}.` {await self.user_name(uid)} (`{uid}`)" for i, uid in enumerate(self.data()["users"], 1)]
        await paginate(ctx, chunk_pages("User whitelist", lines, COLOR, per_page=10,
                                        empty="Nobody has been given access yet (the bot owner always has it)."))


async def setup(bot):
    await bot.add_cog(ServerWhitelist(bot))
