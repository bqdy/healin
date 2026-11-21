"""
Activity Roles (single file)

Tracks messages, voice time, camera time and stream time, and gives or removes
milestone roles when members reach (or no longer meet) a threshold.

Standalone: needs discord.py and aiosqlite. Needs the Server Members Intent.
All commands require "Activity Administrator" (server owner, Administrator
permission, or someone added with -activityadmin add).
"""
import copy
import json
import time

import aiosqlite
import discord
from discord.ext import commands, tasks


class Store:
    """Tiny per-guild JSON key/value store on top of SQLite."""

    def __init__(self, path="activity_roles.db"):
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
        await self.set_many(guild_id, {key: value})

    async def set_many(self, guild_id, items):
        if not items:
            return
        await self.db.executemany(
            "INSERT INTO kv (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            [(guild_id, k, json.dumps(v)) for k, v in items.items()],
        )
        await self.db.commit()

    async def prefix(self, guild_id, prefix):
        async with self.db.execute(
            "SELECT key, value FROM kv WHERE guild_id=? AND key LIKE ?", (guild_id, prefix + "%")
        ) as cur:
            rows = await cur.fetchall()
        return {k: json.loads(v) for k, v in rows}

    async def delete_prefix(self, guild_id, prefix):
        await self.db.execute("DELETE FROM kv WHERE guild_id=? AND key LIKE ?", (guild_id, prefix + "%"))
        await self.db.commit()


# type -> (stat field, divisor to get the unit used for thresholds)
TYPES = {
    "voice_hours": ("voice", 3600),
    "camera_time": ("camera", 3600),
    "stream_time": ("stream", 3600),
    "messages": ("messages", 1),
}

DEFAULT_CFG = {
    "enabled": True,
    "interval": 60,      # minutes, 0 = off
    "next": 0,
    "milestones": [],    # {"role": id, "type": str, "threshold": float}
    "promo_log": None,   # promotion / demotion log channel
    "log": None,         # activity log channel
    "admins": [],
    "norole": [],
}


def parse_interval(text):
    t = text.lower().strip()
    if t == "off":
        return 0
    try:
        if t.endswith("h"):
            return int(float(t[:-1]) * 60)
        if t.endswith("m"):
            return int(t[:-1])
        return int(t)
    except ValueError:
        return None


class ActivityRoles(commands.Cog, name="Activity Roles"):
    """Milestone roles from voice, camera, stream and message activity"""

    help_text = """# Activity Roles

**Required:** Activity Administrator

## Milestone Roles
`{p}activityrole` / `{p}activityrole list` - View configured milestone roles
`{p}activityrole add @role <type> <threshold>` - Set a milestone role
Types: `voice_hours`, `camera_time`, `stream_time`, `messages`
`{p}activityrole remove @role <type>` - Remove a milestone role
`{p}activityrole toggle` - Enable or disable the system
`{p}activityrole sweep <off|30m|1h|2h>` - Set the automatic sweep schedule
`{p}activityrole channel set #channel` - Set the promotion and demotion log

## Activity Settings
`{p}activity enable` / `{p}activity disable` - Turn activity tracking on or off
`{p}activity status` - View the activity system status
`{p}activity interval <off|30m|1h|2h|minutes>` - Set the sweep interval
`{p}activitysweep confirm` - Run a milestone-role sweep now
`{p}activitylog set #channel` / `{p}activitylog clear` - Manage the activity log
`{p}activity reset confirm` - Reset this server's activity data

## Administration and Opt-Outs
`{p}activityadmin add @user` / `{p}activityadmin remove @user` - Manage activity administrators
`{p}activityadmin list` - View activity administrators
`{p}norole add @user` - Exclude a member from activity roles
`{p}norole remove @user` - Allow a member to receive activity roles again
`{p}norole list` - View activity-role opt-outs"""

    def __init__(self, bot):
        self.bot = bot
        self.store = Store()
        self.cfgs = {}
        self.stats = {}
        self.dirty = {}
        self.tick.start()

    async def cog_load(self):
        await self.store.start()

    async def cog_unload(self):
        self.tick.cancel()
        await self.flush()
        if self.store.db:
            await self.store.db.close()

    # ---------- permission check (all commands) ----------
    async def cog_check(self, ctx):
        if not ctx.guild:
            return False
        if ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator:
            return True
        cfg = await self.cfg(ctx.guild.id)
        if ctx.author.id in cfg["admins"]:
            return True
        await ctx.send("You need to be an **Activity Administrator** to use this.")
        return False

    # ---------- data helpers ----------
    async def cfg(self, gid):
        if gid not in self.cfgs:
            cfg = copy.deepcopy(DEFAULT_CFG)
            cfg.update(await self.store.get(gid, "config", {}))
            self.cfgs[gid] = cfg
        return self.cfgs[gid]

    async def save(self, gid):
        await self.store.set(gid, "config", self.cfgs[gid])

    async def stats_for(self, gid):
        if gid not in self.stats:
            rows = await self.store.prefix(gid, "stats:")
            self.stats[gid] = {int(k.split(":")[1]): v for k, v in rows.items()}
        return self.stats[gid]

    def bump(self, gid, uid, field, amount):
        s = self.stats[gid].setdefault(uid, {"messages": 0, "voice": 0, "camera": 0, "stream": 0})
        s[field] = s.get(field, 0) + amount
        self.dirty.setdefault(gid, set()).add(uid)

    async def flush(self):
        dirty, self.dirty = self.dirty, {}
        for gid, uids in dirty.items():
            data = self.stats.get(gid, {})
            await self.store.set_many(gid, {f"stats:{u}": data[u] for u in uids if u in data})

    async def send_log(self, guild, key, text):
        cfg = await self.cfg(guild.id)
        channel = guild.get_channel(cfg[key]) if cfg.get(key) else None
        if channel:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    # ---------- tracking ----------
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        cfg = await self.cfg(message.guild.id)
        if not cfg["enabled"]:
            return
        await self.stats_for(message.guild.id)
        self.bump(message.guild.id, message.author.id, "messages", 1)

    @tasks.loop(minutes=1)
    async def tick(self):
        now = time.time()
        for guild in self.bot.guilds:
            cfg = await self.cfg(guild.id)
            if not cfg["enabled"]:
                continue
            await self.stats_for(guild.id)
            for vc in guild.voice_channels:
                if vc == guild.afk_channel:
                    continue
                for m in vc.members:
                    if m.bot or not m.voice:
                        continue
                    self.bump(guild.id, m.id, "voice", 60)
                    if m.voice.self_video:
                        self.bump(guild.id, m.id, "camera", 60)
                    if m.voice.self_stream:
                        self.bump(guild.id, m.id, "stream", 60)

            if cfg["interval"] > 0:
                if cfg["next"] == 0:
                    cfg["next"] = now + cfg["interval"] * 60
                    await self.save(guild.id)
                elif now >= cfg["next"]:
                    cfg["next"] = now + cfg["interval"] * 60
                    await self.save(guild.id)
                    try:
                        await self.sweep(guild)
                    except Exception as e:  # keep the loop alive
                        print(f"activity_roles: sweep failed in {guild.id}: {e!r}")
        await self.flush()

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    # ---------- sweep ----------
    async def sweep(self, guild):
        cfg = await self.cfg(guild.id)
        if not guild.chunked:
            try:
                await guild.chunk()
            except discord.HTTPException:
                pass
        stats = await self.stats_for(guild.id)

        by_role = {}
        for m in cfg["milestones"]:
            by_role.setdefault(m["role"], []).append(m)

        promoted = demoted = 0
        for member in guild.members:
            if member.bot or member.id in cfg["norole"]:
                continue
            s = stats.get(member.id, {})
            for role_id, milestones in by_role.items():
                role = guild.get_role(role_id)
                if role is None or role >= guild.me.top_role:
                    continue
                hit = next(
                    (m for m in milestones
                     if s.get(TYPES[m["type"]][0], 0) / TYPES[m["type"]][1] >= m["threshold"]),
                    None,
                )
                has = role in member.roles
                try:
                    if hit and not has:
                        await member.add_roles(role, reason="Activity milestone reached")
                        promoted += 1
                        await self.send_log(
                            guild, "promo_log",
                            f"⬆️ {member.mention} promoted to {role.mention} "
                            f"({hit['type']} ≥ {hit['threshold']:g})",
                        )
                    elif not hit and has:
                        await member.remove_roles(role, reason="Activity milestone no longer met")
                        demoted += 1
                        await self.send_log(
                            guild, "promo_log",
                            f"⬇️ {member.mention} demoted from {role.mention} (milestone no longer met)",
                        )
                except discord.HTTPException:
                    pass

        await self.send_log(guild, "log", f"🧹 Sweep complete: {promoted} promoted, {demoted} demoted.")
        return promoted, demoted

    # ======================================================================
    # Milestone roles
    # ======================================================================
    @commands.group(name="activityrole", invoke_without_command=True)
    async def activityrole(self, ctx):
        await ctx.invoke(self.ar_list)

    @activityrole.command(name="list")
    async def ar_list(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        lines = [
            f"<@&{m['role']}> — `{m['type']}` ≥ **{m['threshold']:g}**"
            for m in sorted(cfg["milestones"], key=lambda m: (m["type"], m["threshold"]))
        ]
        await ctx.send(embed=discord.Embed(
            title="Milestone roles",
            description="\n".join(lines) or "No milestone roles configured.",
        ))

    @activityrole.command(name="add")
    async def ar_add(self, ctx, role: discord.Role, kind: str, threshold: float):
        kind = kind.lower()
        if kind not in TYPES:
            return await ctx.send(f"Type must be one of: {', '.join(f'`{t}`' for t in TYPES)}")
        if threshold <= 0:
            return await ctx.send("Threshold must be greater than 0.")
        cfg = await self.cfg(ctx.guild.id)
        cfg["milestones"] = [m for m in cfg["milestones"] if not (m["role"] == role.id and m["type"] == kind)]
        cfg["milestones"].append({"role": role.id, "type": kind, "threshold": threshold})
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ {role.mention} is given at **{threshold:g}** `{kind}`.",
                       allowed_mentions=discord.AllowedMentions.none())

    @activityrole.command(name="remove")
    async def ar_remove(self, ctx, role: discord.Role, kind: str):
        kind = kind.lower()
        cfg = await self.cfg(ctx.guild.id)
        new = [m for m in cfg["milestones"] if not (m["role"] == role.id and m["type"] == kind)]
        if len(new) == len(cfg["milestones"]):
            return await ctx.send("No milestone like that.")
        cfg["milestones"] = new
        await self.save(ctx.guild.id)
        await ctx.send("✅ Milestone removed.")

    @activityrole.command(name="toggle")
    async def ar_toggle(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        cfg["enabled"] = not cfg["enabled"]
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ Activity system **{'enabled' if cfg['enabled'] else 'disabled'}**.")

    async def _set_interval(self, ctx, text):
        minutes = parse_interval(text)
        if minutes is None or (0 < minutes < 5):
            return await ctx.send("Use `off`, `30m`, `1h`, `2h`, or a number of minutes (5 or more).")
        cfg = await self.cfg(ctx.guild.id)
        cfg["interval"] = minutes
        cfg["next"] = time.time() + minutes * 60 if minutes else 0
        await self.save(ctx.guild.id)
        await ctx.send("✅ Automatic sweeps **off**." if not minutes else f"✅ Sweeping every **{minutes}** minutes.")
        await self.send_log(ctx.guild, "log", f"⚙️ Sweep interval set to {minutes or 'off'} by {ctx.author.mention}.")

    @activityrole.command(name="sweep")
    async def ar_sweep(self, ctx, interval: str):
        await self._set_interval(ctx, interval)

    @activityrole.group(name="channel", invoke_without_command=True)
    async def ar_channel(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        ch = f"<#{cfg['promo_log']}>" if cfg["promo_log"] else "not set"
        await ctx.send(f"Promotion/demotion log: {ch}")

    @ar_channel.command(name="set")
    async def ar_channel_set(self, ctx, channel: discord.TextChannel):
        cfg = await self.cfg(ctx.guild.id)
        cfg["promo_log"] = channel.id
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ Promotions and demotions will be logged in {channel.mention}.")

    @ar_channel.command(name="clear")
    async def ar_channel_clear(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        cfg["promo_log"] = None
        await self.save(ctx.guild.id)
        await ctx.send("✅ Promotion/demotion log cleared.")

    # ======================================================================
    # Activity settings
    # ======================================================================
    @commands.group(name="activity", invoke_without_command=True)
    async def activity(self, ctx):
        await ctx.invoke(self.activity_status)

    @activity.command(name="enable")
    async def activity_enable(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        cfg["enabled"] = True
        await self.save(ctx.guild.id)
        await ctx.send("✅ Activity tracking **enabled**.")
        await self.send_log(ctx.guild, "log", f"⚙️ Tracking enabled by {ctx.author.mention}.")

    @activity.command(name="disable")
    async def activity_disable(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        cfg["enabled"] = False
        await self.save(ctx.guild.id)
        await ctx.send("✅ Activity tracking **disabled**.")
        await self.send_log(ctx.guild, "log", f"⚙️ Tracking disabled by {ctx.author.mention}.")

    @activity.command(name="status")
    async def activity_status(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        stats = await self.stats_for(ctx.guild.id)
        embed = discord.Embed(title="Activity system status", color=discord.Color.blurple())
        embed.add_field(name="Tracking", value="✅ Enabled" if cfg["enabled"] else "❌ Disabled")
        embed.add_field(name="Sweep interval", value=f"{cfg['interval']} min" if cfg["interval"] else "off")
        embed.add_field(
            name="Next sweep",
            value=f"<t:{int(cfg['next'])}:R>" if cfg["interval"] and cfg["next"] else "—",
        )
        embed.add_field(name="Members tracked", value=str(len(stats)))
        embed.add_field(name="Milestone roles", value=str(len(cfg["milestones"])))
        embed.add_field(name="Admins / opt-outs", value=f"{len(cfg['admins'])} / {len(cfg['norole'])}")
        embed.add_field(name="Promotion log", value=f"<#{cfg['promo_log']}>" if cfg["promo_log"] else "not set")
        embed.add_field(name="Activity log", value=f"<#{cfg['log']}>" if cfg["log"] else "not set")
        await ctx.send(embed=embed)

    @activity.command(name="interval")
    async def activity_interval(self, ctx, interval: str):
        await self._set_interval(ctx, interval)

    @activity.command(name="reset")
    async def activity_reset(self, ctx, confirm: str = None):
        if (confirm or "").lower() != "confirm":
            return await ctx.send(
                f"⚠️ This deletes all tracked activity in this server. "
                f"Run `{ctx.clean_prefix}activity reset confirm` to continue."
            )
        await self.store.delete_prefix(ctx.guild.id, "stats:")
        self.stats[ctx.guild.id] = {}
        self.dirty.pop(ctx.guild.id, None)
        await ctx.send("✅ Activity data reset.")
        await self.send_log(ctx.guild, "log", f"🗑️ Activity data reset by {ctx.author.mention}.")

    @commands.command(name="activitysweep")
    async def activitysweep(self, ctx, confirm: str = None):
        if (confirm or "").lower() != "confirm":
            return await ctx.send(f"Run `{ctx.clean_prefix}activitysweep confirm` to sweep milestone roles now.")
        async with ctx.typing():
            promoted, demoted = await self.sweep(ctx.guild)
        await ctx.send(f"✅ Sweep complete: **{promoted}** promoted, **{demoted}** demoted.")

    @commands.group(name="activitylog", invoke_without_command=True)
    async def activitylog(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        ch = f"<#{cfg['log']}>" if cfg["log"] else "not set"
        await ctx.send(f"Activity log: {ch}")

    @activitylog.command(name="set")
    async def activitylog_set(self, ctx, channel: discord.TextChannel):
        cfg = await self.cfg(ctx.guild.id)
        cfg["log"] = channel.id
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ Activity log set to {channel.mention}.")

    @activitylog.command(name="clear")
    async def activitylog_clear(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        cfg["log"] = None
        await self.save(ctx.guild.id)
        await ctx.send("✅ Activity log cleared.")

    # ======================================================================
    # Administration and opt-outs
    # ======================================================================
    async def _list_edit(self, ctx, key, member, add):
        cfg = await self.cfg(ctx.guild.id)
        has = member.id in cfg[key]
        if add and has:
            return await ctx.send("Already on the list.")
        if not add and not has:
            return await ctx.send("They're not on the list.")
        if add:
            cfg[key].append(member.id)
        else:
            cfg[key].remove(member.id)
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ {member.mention} {'added' if add else 'removed'}.",
                       allowed_mentions=discord.AllowedMentions.none())

    async def _list_show(self, ctx, key, title):
        cfg = await self.cfg(ctx.guild.id)
        await ctx.send(embed=discord.Embed(
            title=title,
            description="\n".join(f"<@{u}>" for u in cfg[key]) or "None.",
        ))

    @commands.group(name="activityadmin", invoke_without_command=True)
    async def activityadmin(self, ctx):
        await self._list_show(ctx, "admins", "Activity administrators")

    @activityadmin.command(name="add")
    async def activityadmin_add(self, ctx, member: discord.Member):
        await self._list_edit(ctx, "admins", member, True)

    @activityadmin.command(name="remove")
    async def activityadmin_remove(self, ctx, member: discord.Member):
        await self._list_edit(ctx, "admins", member, False)

    @activityadmin.command(name="list")
    async def activityadmin_list(self, ctx):
        await self._list_show(ctx, "admins", "Activity administrators")

    @commands.group(name="norole", invoke_without_command=True)
    async def norole(self, ctx):
        await self._list_show(ctx, "norole", "Activity-role opt-outs")

    @norole.command(name="add")
    async def norole_add(self, ctx, member: discord.Member):
        await self._list_edit(ctx, "norole", member, True)

    @norole.command(name="remove")
    async def norole_remove(self, ctx, member: discord.Member):
        await self._list_edit(ctx, "norole", member, False)

    @norole.command(name="list")
    async def norole_list(self, ctx):
        await self._list_show(ctx, "norole", "Activity-role opt-outs")


async def setup(bot):
    cog = ActivityRoles(bot)
    # Skip any command another cog already registered, so we never clash.
    kept, skipped = [], []
    for cmd in cog.__cog_commands__:
        if any(bot.get_command(n) for n in (cmd.name, *cmd.aliases)):
            skipped.append(cmd.name)
        else:
            kept.append(cmd)
    cog.__cog_commands__ = tuple(kept)
    if skipped:
        print(f"activity_roles: skipped (already exist): {', '.join(skipped)}")
    await bot.add_cog(cog)
