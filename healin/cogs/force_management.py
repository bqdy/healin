import os
import re
import time

import discord
from discord.ext import commands

from utils.punish import notify
from utils.store import GuildStore

COLOR = 0x0F0F10
NO_PINGS = discord.AllowedMentions.none()

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "482506940027502597"))

# Same levels as cogs/rank_management.py
LEVEL_BOT_OWNER = 100
LEVEL_SERVER_OWNER = 90
LEVEL_GOD = 60
LEVEL_MEMBER = 10

ROLE_MENTION_RE = re.compile(r"^<@&(\d+)>$")

DEFAULTS = {
    "bans": {},  # user id -> {"reason": str, "by": int, "at": unix time}
    "nicks": {},  # user id -> forced nickname
    "strips": {},  # user id -> [role ids they may not keep]
}


def find_role(guild: discord.Guild, text: str):
    """Role mention, ID, exact name, then a name that starts with / contains the text."""
    text = text.strip()
    if not text:
        return None
    m = ROLE_MENTION_RE.match(text)
    if m:
        return guild.get_role(int(m.group(1)))
    if text.isdigit():
        role = guild.get_role(int(text))
        if role:
            return role

    roles = [r for r in guild.roles if not r.is_default()]
    low = text.lower()
    for r in roles:
        if r.name.lower() == low:
            return r
    starts = [r for r in roles if r.name.lower().startswith(low)]
    if starts:
        return min(starts, key=lambda r: len(r.name))
    contains = [r for r in roles if low in r.name.lower()]
    if contains:
        return min(contains, key=lambda r: len(r.name))
    return None


class ForceManagement(commands.Cog, name="Force Management"):
    """Forever bans, nicknames, and role strips"""

    help_text = """# Force Management

**Required:** Need God or higher.


## Forever Bans

`{p}foreverban @user [reason]` - Ban a user and automatically ban them again if they rejoin or are unbanned
`{p}unforeverban @user` - Remove the forever ban and unban the user
`{p}foreverbanlist` - View persistent forever bans


## Forced Nicknames

`{p}forcenickname @user <nickname>` - Set and persistently enforce a nickname
`{p}unforcenickname @user` - Stop nickname enforcement


## Forced Role Strips

`{p}forcerolestrip @user @role` - Persistently prevent a user from keeping a role
`{p}forcestrip @user @role` - Same persistent user role strip
`{p}forcestrip <role-name/id>` - Remove a role from all eligible members once
`{p}unforcerolestrip @user` - Remove all persistent role-strip rules for a user
`{p}unforcestrip @user` - Shortcut for `{p}unforcerolestrip`"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GuildStore("force_management", DEFAULTS)

    def cfg(self, guild: discord.Guild) -> dict:
        return self.store.get(guild.id)

    # ------------------------------------------------------------------ plumbing

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}"
            return await ctx.send(f"Usage: `{usage}`")
        if isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                return await ctx.send(
                    "I don't have permission to do that. I need Ban Members / Manage Nicknames / "
                    "Manage Roles, and my role has to be above the target."
                )
            if isinstance(original, discord.HTTPException):
                return await ctx.send(f"Discord rejected that: {original.text or original}")
        raise error

    # ------------------------------------------------------------------- helpers

    def level(self, guild: discord.Guild, user_id: int) -> int:
        ranks = self.bot.get_cog("Rank Management")
        if ranks is not None:
            return ranks.level(guild, user_id)
        if user_id == BOT_OWNER_ID:
            return LEVEL_BOT_OWNER
        if user_id == guild.owner_id:
            return LEVEL_SERVER_OWNER
        return LEVEL_MEMBER

    async def need_god(self, ctx: commands.Context) -> bool:
        if self.level(ctx.guild, ctx.author.id) >= LEVEL_GOD:
            return True
        await ctx.send("You need the **God** rank or above to use that.")
        return False

    def outranks(self, guild: discord.Guild, caller_id: int, target_id: int) -> bool:
        return caller_id == BOT_OWNER_ID or self.level(guild, caller_id) > self.level(guild, target_id)

    async def can_touch(self, ctx: commands.Context, user_id: int) -> bool:
        """God+ check plus: not the bot, not the owners, not someone at or above your rank."""
        if user_id == self.bot.user.id:
            return False
        if user_id in (BOT_OWNER_ID, ctx.guild.owner_id) and ctx.author.id != BOT_OWNER_ID:
            await ctx.send("You can't do that to the server owner or bot owner.")
            return False
        if not self.outranks(ctx.guild, ctx.author.id, user_id):
            await ctx.send("You can't do that to someone at or above your rank.")
            return False
        return True

    def below_bot(self, guild: discord.Guild, member: discord.Member) -> bool:
        return member.id != guild.owner_id and member.top_role < guild.me.top_role

    # ============================================================ forever bans

    @commands.command(name="foreverban")
    async def foreverban(self, ctx: commands.Context, user: discord.User, *, reason: str = None):
        """Ban a user and automatically ban them again if they rejoin or are unbanned"""
        if not await self.need_god(ctx):
            return
        if not await self.can_touch(ctx, user.id):
            return

        member = ctx.guild.get_member(user.id)
        if member and not self.below_bot(ctx.guild, member):
            return await ctx.send("That member is above my top role, so I can't ban them.")

        cfg = self.cfg(ctx.guild)
        if str(user.id) in cfg["bans"]:
            return await ctx.send(f"**{user}** already has a forever ban.", allowed_mentions=NO_PINGS)

        reason = (reason or "No reason given")[:400]
        cfg["bans"][str(user.id)] = {"reason": reason, "by": ctx.author.id, "at": int(time.time())}
        self.store.save()

        try:
            async with notify(ctx, user, "ban", reason):
                await ctx.guild.ban(user, reason=f"Forever ban by {ctx.author}: {reason}"[:512], delete_message_seconds=0)
        except discord.HTTPException:
            # keep the record only if the ban actually happened
            del cfg["bans"][str(user.id)]
            self.store.save()
            raise
        await ctx.send(
            f"**{user}** has been forever banned. They'll be banned again if they're unbanned or rejoin.",
            allowed_mentions=NO_PINGS,
        )

    @commands.command(name="unforeverban")
    async def unforeverban(self, ctx: commands.Context, user: discord.User):
        """Remove the forever ban and unban the user"""
        if not await self.need_god(ctx):
            return
        cfg = self.cfg(ctx.guild)
        if str(user.id) not in cfg["bans"]:
            return await ctx.send(f"**{user}** doesn't have a forever ban.", allowed_mentions=NO_PINGS)

        # drop the record first so the unban listener doesn't ban them again
        del cfg["bans"][str(user.id)]
        self.store.save()
        try:
            await ctx.guild.unban(user, reason=f"Forever ban removed by {ctx.author}")
        except discord.NotFound:
            pass  # they were already unbanned
        await ctx.send(f"Removed the forever ban on **{user}** and unbanned them.", allowed_mentions=NO_PINGS)

    @commands.command(name="foreverbanlist")
    async def foreverbanlist(self, ctx: commands.Context):
        """View persistent forever bans"""
        if not await self.need_god(ctx):
            return
        bans = self.cfg(ctx.guild)["bans"]
        lines, shown = [], 0
        for uid, info in bans.items():
            line = f"<@{uid}> (`{uid}`) - {info['reason']} - by <@{info['by']}> <t:{info['at']}:R>"
            if sum(len(l) + 1 for l in lines) + len(line) > 3800:
                break
            lines.append(line)
            shown += 1
        text = "\n".join(lines) or "No forever bans."
        if shown < len(bans):
            text += f"\n...and {len(bans) - shown} more"
        await ctx.send(
            embed=discord.Embed(title=f"Forever bans ({len(bans)})", description=text, color=COLOR),
            allowed_mentions=NO_PINGS,
        )

    # ======================================================== forced nicknames

    @commands.command(name="forcenickname")
    async def forcenickname(self, ctx: commands.Context, member: discord.Member, *, nickname: str):
        """Set and persistently enforce a nickname"""
        if not await self.need_god(ctx):
            return
        if not await self.can_touch(ctx, member.id):
            return
        if len(nickname) > 32:
            return await ctx.send("Nicknames can be at most 32 characters.")
        if not self.below_bot(ctx.guild, member):
            return await ctx.send("That member is above my top role, so I can't change their nickname.")

        cfg = self.cfg(ctx.guild)
        cfg["nicks"][str(member.id)] = nickname
        self.store.save()
        await member.edit(nick=nickname, reason=f"Forced nickname by {ctx.author}")
        await ctx.send(
            f"**{member.name}**'s nickname is now locked to **{nickname}**.", allowed_mentions=NO_PINGS
        )

    @commands.command(name="unforcenickname")
    async def unforcenickname(self, ctx: commands.Context, user: discord.User):
        """Stop nickname enforcement"""
        if not await self.need_god(ctx):
            return
        cfg = self.cfg(ctx.guild)
        if str(user.id) not in cfg["nicks"]:
            return await ctx.send(f"**{user}** has no forced nickname.", allowed_mentions=NO_PINGS)
        del cfg["nicks"][str(user.id)]
        self.store.save()
        await ctx.send(f"Stopped enforcing a nickname for **{user}**.", allowed_mentions=NO_PINGS)

    # ======================================================= forced role strips

    async def _persistent_strip(self, ctx: commands.Context, member: discord.Member, role: discord.Role):
        if role.is_default() or role.managed:
            return await ctx.send("You can't strip @everyone or bot/integration roles.")
        if role >= ctx.guild.me.top_role:
            return await ctx.send(f"**{role.name}** is at or above my top role, so I can't remove it.", allowed_mentions=NO_PINGS)
        if not await self.can_touch(ctx, member.id):
            return

        cfg = self.cfg(ctx.guild)
        ids = cfg["strips"].setdefault(str(member.id), [])
        if role.id in ids:
            return await ctx.send(
                f"**{member.name}** is already barred from **{role.name}**.", allowed_mentions=NO_PINGS
            )
        ids.append(role.id)
        self.store.save()

        if role in member.roles:
            await member.remove_roles(role, reason=f"Forced role strip by {ctx.author}")
        await ctx.send(
            f"**{member.name}** can no longer keep **{role.name}**. It'll be removed whenever they get it.",
            allowed_mentions=NO_PINGS,
        )

    @commands.command(name="forcerolestrip")
    async def forcerolestrip(self, ctx: commands.Context, member: discord.Member, *, role: str):
        """Persistently prevent a user from keeping a role"""
        if not await self.need_god(ctx):
            return
        found = find_role(ctx.guild, role)
        if found is None:
            return await ctx.send(f"No role matching `{role}`.")
        await self._persistent_strip(ctx, member, found)

    @commands.command(name="forcestrip")
    async def forcestrip(self, ctx: commands.Context, *, args: str):
        """`forcestrip @user @role` = persistent strip, `forcestrip <role>` = remove from everyone once"""
        if not await self.need_god(ctx):
            return

        # "@user @role": the first word is a member and something follows it
        parts = args.split(None, 1)
        if len(parts) == 2:
            try:
                member = await commands.MemberConverter().convert(ctx, parts[0])
            except commands.BadArgument:
                member = None
            if member is not None:
                role = find_role(ctx.guild, parts[1])
                if role is None:
                    return await ctx.send(f"No role matching `{parts[1]}`.")
                return await self._persistent_strip(ctx, member, role)

        # "<role-name/id>": take the role off every member we're allowed to touch, once
        role = find_role(ctx.guild, args)
        if role is None:
            return await ctx.send(f"No role matching `{args}`.")
        if role.is_default() or role.managed:
            return await ctx.send("You can't strip @everyone or bot/integration roles.")
        if role >= ctx.guild.me.top_role:
            return await ctx.send(f"**{role.name}** is at or above my top role, so I can't remove it.", allowed_mentions=NO_PINGS)
        if not self.bot.intents.members:
            return await ctx.send("This needs the Server Members intent (`intents.members = True` in `main.py`).")

        removed = skipped = failed = 0
        for member in list(role.members):
            if member.bot or not self.outranks(ctx.guild, ctx.author.id, member.id) or member.id in (
                ctx.guild.owner_id,
                BOT_OWNER_ID,
            ):
                skipped += 1
                continue
            try:
                await member.remove_roles(role, reason=f"Force strip by {ctx.author}")
                removed += 1
            except discord.HTTPException:
                failed += 1

        text = f"Removed **{role.name}** from **{removed}** member(s)."
        if skipped:
            text += f" Skipped {skipped} (bots or equal/higher rank)."
        if failed:
            text += f" {failed} failed."
        await ctx.send(text, allowed_mentions=NO_PINGS)

    @commands.command(name="unforcerolestrip", aliases=["unforcestrip"])
    async def unforcerolestrip(self, ctx: commands.Context, user: discord.User):
        """Remove all persistent role-strip rules for a user"""
        if not await self.need_god(ctx):
            return
        cfg = self.cfg(ctx.guild)
        if not cfg["strips"].pop(str(user.id), None):
            return await ctx.send(f"**{user}** has no role-strip rules.", allowed_mentions=NO_PINGS)
        self.store.save()
        await ctx.send(f"Removed all role-strip rules for **{user}**.", allowed_mentions=NO_PINGS)

    # ================================================================ enforcement

    async def enforce(self, member: discord.Member):
        """Put a member's forced nickname back and strip any barred roles."""
        guild = member.guild
        cfg = self.cfg(guild)
        uid = str(member.id)

        forced = cfg["nicks"].get(uid)
        if forced is not None and member.nick != forced and self.below_bot(guild, member):
            try:
                await member.edit(nick=forced, reason="Forced nickname")
            except discord.HTTPException:
                pass

        barred = cfg["strips"].get(uid)
        if barred:
            bad = [r for r in member.roles if r.id in barred and r < guild.me.top_role]
            if bad:
                try:
                    await member.remove_roles(*bad, reason="Forced role strip")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick or before.roles != after.roles:
            await self.enforce(after)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.cfg(member.guild)
        info = cfg["bans"].get(str(member.id))
        if info:
            try:
                await member.guild.ban(member, reason=f"Forever ban: {info['reason']}"[:512])
            except discord.HTTPException:
                pass
            return
        await self.enforce(member)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        info = self.cfg(guild)["bans"].get(str(user.id))
        if info:
            try:
                await guild.ban(user, reason=f"Forever ban restored: {info['reason']}"[:512])
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ForceManagement(bot))
