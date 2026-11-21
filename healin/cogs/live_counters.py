"""
Live Channel Counters (single file)

-counters add members [category]
-counters add boosters [category]
-counters add membergoal [category] --goal 1000
-counters add boostgoal [category] --goal 50
-counters list
-counters remove <channel>

Standalone: needs discord.py and aiosqlite. Counters sync every 10 minutes.
Bot needs Manage Channels; booster counters need the Server Members Intent.
"""
import json
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands, tasks


class Store:
    """Tiny per-guild JSON key/value store on top of SQLite."""

    def __init__(self, path="live_counters.db"):
        self.path = path
        self.db = None

    async def start(self):
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (guild_id, key))"
        )
        await self.db.commit()

    async def get(self, guild_id, key, default=None):
        async with self.db.execute(
            "SELECT value FROM kv WHERE guild_id=? AND key=?", (guild_id, key)
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else default

    async def set(self, guild_id, key, value):
        await self.db.execute(
            "INSERT INTO kv (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, json.dumps(value)),
        )
        await self.db.commit()


TEMPLATES = {
    "members": "Members: {n}",
    "boosters": "Boosters: {n}",
    "membergoal": "Member Goal: {n}/{goal}",
    "boostgoal": "Boost Goal: {n}/{goal}",
}


def current_value(guild, kind):
    if kind in ("members", "membergoal"):
        return guild.member_count or 0
    if kind == "boosters":
        return len(guild.premium_subscribers)  # members who are boosting
    return guild.premium_subscription_count or 0  # boostgoal: total boosts


def counter_name(guild, kind, goal=None):
    return TEMPLATES[kind].format(n=current_value(guild, kind), goal=goal)


class GoalFlags(commands.FlagConverter, prefix="--", delimiter=" "):
    goal: Optional[int] = None


class LiveChannelCounters(commands.Cog, name="Live Channel Counters"):
    """Live member and booster counter channels"""

    help_text = """# Live Channel Counters

`{p}counters add members [category]` - Create a member counter
`{p}counters add boosters [category]` - Create a boosting-member counter
`{p}counters add membergoal [category] --goal 1000` - Create a member goal
`{p}counters add boostgoal [category] --goal 50` - Create a booster goal
`{p}counters list` - List configured counters
`{p}counters remove <channel>` - Delete a counter channel

Counters sync every 10 minutes and block regular members from joining.
Requires Manage Server. The bot needs Manage Channels; booster counters need Server Members Intent."""

    def __init__(self, bot):
        self.bot = bot
        self.store = Store()
        self.sync_loop.start()

    async def cog_load(self):
        await self.store.start()

    async def cog_unload(self):
        self.sync_loop.cancel()
        if self.store.db:
            await self.store.db.close()

    # ---------- syncing ----------
    async def sync_guild(self, guild):
        items = await self.store.get(guild.id, "counters", [])
        keep = []
        for item in items:
            channel = guild.get_channel(item["channel"])
            if channel is None:  # deleted manually: forget it
                continue
            keep.append(item)
            name = counter_name(guild, item["kind"], item.get("goal"))
            if channel.name != name:
                try:
                    await channel.edit(name=name, reason="Live counter sync")
                except discord.HTTPException:
                    pass
        if len(keep) != len(items):
            await self.store.set(guild.id, "counters", keep)

    @tasks.loop(minutes=10)
    async def sync_loop(self):
        for guild in self.bot.guilds:
            await self.sync_guild(guild)

    @sync_loop.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    async def create(self, ctx, kind, category, goal=None):
        if kind.endswith("goal") and (goal is None or goal < 1):
            return await ctx.send(f"Add a goal, e.g. `{ctx.clean_prefix}counters add {kind} --goal 100`")
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True)
        }
        channel = await ctx.guild.create_voice_channel(
            counter_name(ctx.guild, kind, goal),
            category=category,
            overwrites=overwrites,
            reason=f"Live counter ({kind}) by {ctx.author}",
        )
        items = await self.store.get(ctx.guild.id, "counters", [])
        items.append({"channel": channel.id, "kind": kind, "goal": goal})
        await self.store.set(ctx.guild.id, "counters", items)
        await ctx.send(f"✅ Created {channel.mention} ({kind}). It updates every 10 minutes.")

    # ---------- commands ----------
    @commands.group(name="counters", invoke_without_command=True)
    async def counters(self, ctx):
        text = self.help_text.replace("{p}", ctx.clean_prefix).split("\n", 2)[2]
        await ctx.send(embed=discord.Embed(title="Live Channel Counters", description=text,
                                           color=discord.Color.blurple()))

    @counters.group(name="add", invoke_without_command=True)
    async def counters_add(self, ctx):
        await self.counters(ctx)

    @counters_add.command(name="members")
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def add_members(self, ctx, category: Optional[discord.CategoryChannel] = None):
        await self.create(ctx, "members", category)

    @counters_add.command(name="boosters")
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def add_boosters(self, ctx, category: Optional[discord.CategoryChannel] = None):
        await self.create(ctx, "boosters", category)

    @counters_add.command(name="membergoal")
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def add_membergoal(self, ctx, category: Optional[discord.CategoryChannel] = None,
                             *, flags: GoalFlags):
        await self.create(ctx, "membergoal", category, flags.goal)

    @counters_add.command(name="boostgoal")
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def add_boostgoal(self, ctx, category: Optional[discord.CategoryChannel] = None,
                            *, flags: GoalFlags):
        await self.create(ctx, "boostgoal", category, flags.goal)

    @counters.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def counters_list(self, ctx):
        items = await self.store.get(ctx.guild.id, "counters", [])
        lines = []
        for item in items:
            channel = ctx.guild.get_channel(item["channel"])
            goal = f" (goal {item['goal']})" if item.get("goal") else ""
            lines.append(f"{channel.mention if channel else '*deleted*'} — `{item['kind']}`{goal}")
        await ctx.send(embed=discord.Embed(
            title="Configured counters",
            description="\n".join(lines) or "No counters yet.",
        ))

    @counters.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def counters_remove(self, ctx, channel: discord.abc.GuildChannel):
        items = await self.store.get(ctx.guild.id, "counters", [])
        new = [i for i in items if i["channel"] != channel.id]
        if len(new) == len(items):
            return await ctx.send("That isn't a counter channel.")
        await self.store.set(ctx.guild.id, "counters", new)
        try:
            await channel.delete(reason=f"Counter removed by {ctx.author}")
        except discord.HTTPException:
            pass
        await ctx.send("✅ Counter removed.")


async def setup(bot):
    cog = LiveChannelCounters(bot)
    # Skip any command another cog already registered, so we never clash.
    kept, skipped = [], []
    for cmd in cog.__cog_commands__:
        if any(bot.get_command(n) for n in (cmd.name, *cmd.aliases)):
            skipped.append(cmd.name)
        else:
            kept.append(cmd)
    cog.__cog_commands__ = tuple(kept)
    if skipped:
        print(f"live_counters: skipped (already exist): {', '.join(skipped)}")
    await bot.add_cog(cog)
