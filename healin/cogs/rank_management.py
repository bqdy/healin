import os

import discord
from discord.ext import commands

from utils.store import GuildStore

NO_PINGS = discord.AllowedMentions.none()

# The bot owner (same ID as in swl.py). Can be overridden with BOT_OWNER_ID in .env.
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "482506940027502597"))

# key -> (display name, level). Higher level = higher rank.
# Bot Owner (100) and Server Owner (90) aren't stored; they're detected live.
RANKS = {
    "ceo": ("CEO", 80),
    "fool": ("Fool", 70),
    "god": ("God", 60),
    "pres": ("Pres", 50),
    "party": ("Party", 40),
    "clover": ("Clover", 30),
    "cheif": ("Cheif", 20),
}
LEVEL_BOT_OWNER = 100
LEVEL_SERVER_OWNER = 90
LEVEL_CEO = RANKS["ceo"][1]
LEVEL_MEMBER = 10

DEFAULTS = {
    "roles": {},  # rank key -> role id
    "members": {},  # user id -> rank key
}


class RankManagement(commands.Cog, name="Rank Management"):
    """Assign, remove, and list server ranks"""

    help_text = """# Rank Management

**Bot Owner:**
`{p}ceo add @user` / `{p}ceo remove @user`

**Server Owner / Bot Owner / Ceo:**
`{p}fool add @user` / `{p}fool remove @user`
`{p}god add @user` / `{p}god remove @user`
`{p}pres add @user` / `{p}pres remove @user`
`{p}party add @user` / `{p}party remove @user`
`{p}clover add @user` / `{p}clover remove @user`
`{p}cheif add @user` / `{p}chief remove @user`

**Server Owner / Bot Owner:**
`{p}clearranks @user` - Clear a member's rank


**Rank Hierarchy (Highest to Lowest):**
1. Bot Owner
2. Server Owner
3. CEO
4. Fool
5. God
6. Pres
7. Party
8. Clover
9. Cheif
10. Member"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GuildStore("ranks", DEFAULTS)

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
                    "I don't have permission to do that. I need Manage Roles, and my role has to be "
                    "above the rank roles."
                )
            if isinstance(original, discord.HTTPException):
                return await ctx.send(f"Discord rejected that: {original.text or original}")
        raise error

    # ------------------------------------------------------------------- helpers

    def rank_of(self, guild: discord.Guild, user_id: int):
        return self.cfg(guild)["members"].get(str(user_id))

    def level(self, guild: discord.Guild, user_id: int) -> int:
        if user_id == BOT_OWNER_ID:
            return LEVEL_BOT_OWNER
        if user_id == guild.owner_id:
            return LEVEL_SERVER_OWNER
        key = self.rank_of(guild, user_id)
        return RANKS[key][1] if key in RANKS else LEVEL_MEMBER

    def rank_roles(self, guild: discord.Guild) -> list:
        roles = self.cfg(guild)["roles"]
        found = (guild.get_role(rid) for rid in roles.values())
        return [r for r in found if r is not None]

    async def ensure_role(self, guild: discord.Guild, key: str) -> discord.Role:
        """The role for a rank: the saved one, one with the same name, or a new one."""
        cfg = self.cfg(guild)
        name = RANKS[key][0]

        role = guild.get_role(cfg["roles"].get(key, 0))
        if role is None:
            role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role is None:
            role = await guild.create_role(name=name, reason="Rank Management: creating rank role")

        cfg["roles"][key] = role.id
        self.store.save()
        return role

    async def manage(self, ctx: commands.Context, key: str, member: discord.Member, add: bool):
        guild = ctx.guild
        display = RANKS[key][0]
        caller = self.level(guild, ctx.author.id)
        target = self.level(guild, member.id)

        # who may touch this rank
        if key == "ceo":
            if ctx.author.id != BOT_OWNER_ID:
                return await ctx.send("Only the bot owner can manage the CEO rank.")
        elif caller < LEVEL_CEO:
            return await ctx.send("Only the server owner, bot owner or a CEO can manage ranks.")

        if member.bot:
            return await ctx.send("Bots can't have ranks.")
        if target >= LEVEL_SERVER_OWNER:
            return await ctx.send("You can't change the rank of the server owner or bot owner.")
        if ctx.author.id != BOT_OWNER_ID and target >= caller:
            return await ctx.send("You can't change the rank of someone at or above your own rank.")

        cfg = self.cfg(guild)
        current = cfg["members"].get(str(member.id))
        reason = f"Rank Management: by {ctx.author} ({ctx.author.id})"

        if add:
            if current == key:
                return await ctx.send(
                    f"**{member.display_name}** is already **{display}**.", allowed_mentions=NO_PINGS
                )

            role = await self.ensure_role(guild, key)
            # one rank at a time: drop whichever rank they had
            old = [r for r in self.rank_roles(guild) if r in member.roles and r.id != role.id]
            if old:
                await member.remove_roles(*old, reason=reason)
            await member.add_roles(role, reason=reason)

            cfg["members"][str(member.id)] = key
            self.store.save()
            return await ctx.send(
                f"**{member.display_name}** is now **{display}**.", allowed_mentions=NO_PINGS
            )

        # remove
        if current != key:
            return await ctx.send(
                f"**{member.display_name}** isn't **{display}**.", allowed_mentions=NO_PINGS
            )
        role = guild.get_role(cfg["roles"].get(key, 0))
        if role and role in member.roles:
            await member.remove_roles(role, reason=reason)
        del cfg["members"][str(member.id)]
        self.store.save()
        await ctx.send(
            f"Removed **{display}** from **{member.display_name}**.", allowed_mentions=NO_PINGS
        )

    # ------------------------------------------------------------------ commands

    async def usage(self, ctx: commands.Context, name: str):
        await ctx.send(f"Usage: `{ctx.prefix}{name} add @user` or `{ctx.prefix}{name} remove @user`")

    @commands.group(name="ceo", invoke_without_command=True)
    async def ceo(self, ctx: commands.Context):
        """Manage the CEO rank (bot owner only)"""
        await self.usage(ctx, "ceo")

    @ceo.command(name="add")
    async def ceo_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the CEO rank"""
        await self.manage(ctx, "ceo", member, True)

    @ceo.command(name="remove")
    async def ceo_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the CEO rank from a member"""
        await self.manage(ctx, "ceo", member, False)

    @commands.group(name="fool", invoke_without_command=True)
    async def fool(self, ctx: commands.Context):
        """Manage the Fool rank"""
        await self.usage(ctx, "fool")

    @fool.command(name="add")
    async def fool_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the Fool rank"""
        await self.manage(ctx, "fool", member, True)

    @fool.command(name="remove")
    async def fool_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the Fool rank from a member"""
        await self.manage(ctx, "fool", member, False)

    @commands.group(name="god", invoke_without_command=True)
    async def god(self, ctx: commands.Context):
        """Manage the God rank"""
        await self.usage(ctx, "god")

    @god.command(name="add")
    async def god_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the God rank"""
        await self.manage(ctx, "god", member, True)

    @god.command(name="remove")
    async def god_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the God rank from a member"""
        await self.manage(ctx, "god", member, False)

    @commands.group(name="pres", invoke_without_command=True)
    async def pres(self, ctx: commands.Context):
        """Manage the Pres rank"""
        await self.usage(ctx, "pres")

    @pres.command(name="add")
    async def pres_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the Pres rank"""
        await self.manage(ctx, "pres", member, True)

    @pres.command(name="remove")
    async def pres_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the Pres rank from a member"""
        await self.manage(ctx, "pres", member, False)

    @commands.group(name="party", invoke_without_command=True)
    async def party(self, ctx: commands.Context):
        """Manage the Party rank"""
        await self.usage(ctx, "party")

    @party.command(name="add")
    async def party_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the Party rank"""
        await self.manage(ctx, "party", member, True)

    @party.command(name="remove")
    async def party_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the Party rank from a member"""
        await self.manage(ctx, "party", member, False)

    @commands.group(name="clover", invoke_without_command=True)
    async def clover(self, ctx: commands.Context):
        """Manage the Clover rank"""
        await self.usage(ctx, "clover")

    @clover.command(name="add")
    async def clover_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the Clover rank"""
        await self.manage(ctx, "clover", member, True)

    @clover.command(name="remove")
    async def clover_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the Clover rank from a member"""
        await self.manage(ctx, "clover", member, False)

    # the screenshot spells it both ways, so both work
    @commands.group(name="cheif", aliases=["chief"], invoke_without_command=True)
    async def cheif(self, ctx: commands.Context):
        """Manage the Cheif rank"""
        await self.usage(ctx, "cheif")

    @cheif.command(name="add")
    async def cheif_add(self, ctx: commands.Context, member: discord.Member):
        """Give a member the Cheif rank"""
        await self.manage(ctx, "cheif", member, True)

    @cheif.command(name="remove")
    async def cheif_remove(self, ctx: commands.Context, member: discord.Member):
        """Take the Cheif rank from a member"""
        await self.manage(ctx, "cheif", member, False)

    @commands.command(name="clearranks")
    async def clearranks(self, ctx: commands.Context, member: discord.Member):
        """Clear a member's rank"""
        guild = ctx.guild
        if self.level(guild, ctx.author.id) < LEVEL_SERVER_OWNER:
            return await ctx.send("Only the server owner or bot owner can clear ranks.")
        if self.level(guild, member.id) >= LEVEL_SERVER_OWNER and member.id != ctx.author.id:
            return await ctx.send("You can't clear the rank of the server owner or bot owner.")

        cfg = self.cfg(guild)
        had = cfg["members"].pop(str(member.id), None)
        held = [r for r in self.rank_roles(guild) if r in member.roles]
        if held:
            await member.remove_roles(*held, reason=f"Rank Management: cleared by {ctx.author}")
        self.store.save()

        if had is None and not held:
            return await ctx.send(
                f"**{member.display_name}** has no rank.", allowed_mentions=NO_PINGS
            )
        await ctx.send(
            f"Cleared the rank for **{member.display_name}**.", allowed_mentions=NO_PINGS
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RankManagement(bot))
