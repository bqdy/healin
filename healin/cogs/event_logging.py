"""
Event Logging (single file)

-logging                          view configured log channels
-logging set <category> [#channel]   set one category
-logging set all [#channel]          set every category
-logging remove <category>           remove one category
-logging remove all                  remove all log channels
-logging test <category>             send a test log
-logging help                        view logging help

Categories: message, voice, channel, role, server, member

Standalone: needs discord.py and aiosqlite. Needs the Server Members and
Message Content intents; the bot needs View Audit Log to show who did things.
"""
import asyncio
import json
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands

CATEGORIES = ["message", "voice", "channel", "role", "server", "member"]

GREEN = discord.Color.green()
RED = discord.Color.red()
ORANGE = discord.Color.orange()
BLUE = discord.Color.blurple()


class Store:
    """Tiny per-guild JSON key/value store on top of SQLite."""

    def __init__(self, path="event_logging.db"):
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


def cut(text, n=1000):
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def make(title, color, description=None, fields=None, footer=None):
    e = discord.Embed(title=title, description=description, color=color,
                      timestamp=discord.utils.utcnow())
    for name, value, inline in fields or []:
        e.add_field(name=name, value=cut(str(value), 1024) or "—", inline=inline)
    if footer:
        e.set_footer(text=footer)
    return e


class EventLogging(commands.Cog, name="Event Logging"):
    """Log messages, voice, channels, roles, server and member events"""

    help_text = """# Event Logging

**Required:** Anti-Nuke Admin
**Categories:** Message, Voice, Channel, Role, Server, Member

`{p}logging` - View configured log channels
`{p}logging set <category> [#channel]` - Set one category
`{p}logging set all [#channel]` - Set every category
`{p}logging remove <category>` - Remove one category
`{p}logging remove all` - Remove all log channels
`{p}logging test <category>` - Send a test log
`{p}logging help` - View logging help"""

    def __init__(self, bot):
        self.bot = bot
        self.store = Store()
        self.cfgs = {}

    async def cog_load(self):
        await self.store.start()

    async def cog_unload(self):
        if self.store.db:
            await self.store.db.close()

    # ---------- permission check ----------
    async def cog_check(self, ctx):
        if not ctx.guild:
            return False
        if ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator:
            return True
        await ctx.send("You need to be an **Anti-Nuke Admin** to use this.")
        return False

    # ---------- helpers ----------
    async def cfg(self, gid):
        if gid not in self.cfgs:
            self.cfgs[gid] = await self.store.get(gid, "channels", {})
        return self.cfgs[gid]

    async def save(self, gid):
        await self.store.set(gid, "channels", self.cfgs[gid])

    async def log(self, guild, category, embed):
        cfg = await self.cfg(guild.id)
        channel_id = cfg.get(category)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    async def is_log_channel(self, guild, channel_id):
        return channel_id in (await self.cfg(guild.id)).values()

    async def executor(self, guild, action, target_id=None):
        """Who did it, according to the audit log (needs View Audit Log)."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1)
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                if (discord.utils.utcnow() - entry.created_at).total_seconds() > 15:
                    break
                if target_id is None or (entry.target and getattr(entry.target, "id", None) == target_id):
                    return entry.user
        except discord.HTTPException:
            pass
        return None

    @staticmethod
    def by(user):
        return [("By", user.mention, True)] if user else []

    @staticmethod
    def norm(category):
        c = category.lower()
        if c.endswith("s") and c[:-1] in CATEGORIES:
            c = c[:-1]
        return c

    # ======================================================================
    # Commands
    # ======================================================================
    @commands.group(name="logging", invoke_without_command=True)
    async def logging_group(self, ctx):
        cfg = await self.cfg(ctx.guild.id)
        lines = [
            f"**{c.title()}** → " + (f"<#{cfg[c]}>" if cfg.get(c) else "*not set*")
            for c in CATEGORIES
        ]
        await ctx.send(embed=discord.Embed(
            title="Event logging", description="\n".join(lines), color=BLUE))

    @logging_group.command(name="set")
    async def logging_set(self, ctx, category: str, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        cat = self.norm(category)
        cfg = await self.cfg(ctx.guild.id)
        if cat == "all":
            for c in CATEGORIES:
                cfg[c] = channel.id
            await self.save(ctx.guild.id)
            return await ctx.send(f"✅ All categories will be logged in {channel.mention}.")
        if cat not in CATEGORIES:
            return await ctx.send(f"Category must be one of: {', '.join(f'`{c}`' for c in CATEGORIES)}, or `all`.")
        cfg[cat] = channel.id
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ **{cat.title()}** logs will go to {channel.mention}.")

    @logging_group.command(name="remove")
    async def logging_remove(self, ctx, category: str):
        cat = self.norm(category)
        cfg = await self.cfg(ctx.guild.id)
        if cat == "all":
            cfg.clear()
            await self.save(ctx.guild.id)
            return await ctx.send("✅ All log channels removed.")
        if cat not in CATEGORIES:
            return await ctx.send(f"Category must be one of: {', '.join(f'`{c}`' for c in CATEGORIES)}, or `all`.")
        if cfg.pop(cat, None) is None:
            return await ctx.send("That category isn't set.")
        await self.save(ctx.guild.id)
        await ctx.send(f"✅ **{cat.title()}** logging removed.")

    @logging_group.command(name="test")
    async def logging_test(self, ctx, category: str):
        cat = self.norm(category)
        if cat not in CATEGORIES:
            return await ctx.send(f"Category must be one of: {', '.join(f'`{c}`' for c in CATEGORIES)}.")
        cfg = await self.cfg(ctx.guild.id)
        if not cfg.get(cat):
            return await ctx.send("That category has no log channel yet.")
        await self.log(ctx.guild, cat, make(
            f"🧪 Test log: {cat.title()}", BLUE,
            f"This is a test log requested by {ctx.author.mention}."))
        await ctx.send("✅ Test log sent.")

    @logging_group.command(name="help")
    async def logging_help(self, ctx):
        text = self.help_text.replace("{p}", ctx.clean_prefix).split("\n", 2)[2]
        await ctx.send(embed=discord.Embed(title="Event Logging", description=text, color=BLUE))

    # ======================================================================
    # Message events
    # ======================================================================
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if not message.guild or message.author.bot:
            return
        if await self.is_log_channel(message.guild, message.channel.id):
            return
        fields = [("Author", message.author.mention, True), ("Channel", message.channel.mention, True)]
        if message.attachments:
            fields.append(("Attachments", "\n".join(a.filename for a in message.attachments), False))
        await self.log(message.guild, "message", make(
            "🗑️ Message deleted", RED, cut(message.content) or "*no text*", fields,
            footer=f"User ID: {message.author.id}"))

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        if not messages or not messages[0].guild:
            return
        guild, channel = messages[0].guild, messages[0].channel
        await self.log(guild, "message", make(
            "🗑️ Bulk delete", RED, f"**{len(messages)}** messages deleted in {channel.mention}."))

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        if await self.is_log_channel(after.guild, after.channel.id):
            return
        await self.log(after.guild, "message", make(
            "✏️ Message edited", ORANGE,
            f"[Jump to message]({after.jump_url})",
            [("Author", after.author.mention, True), ("Channel", after.channel.mention, True),
             ("Before", cut(before.content) or "*not cached*", False),
             ("After", cut(after.content) or "*empty*", False)],
            footer=f"User ID: {after.author.id}"))

    # ======================================================================
    # Voice events
    # ======================================================================
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        if before.channel != after.channel:
            if before.channel is None:
                title, color, desc = "🔊 Joined voice", GREEN, f"{member.mention} joined {after.channel.mention}"
            elif after.channel is None:
                title, color, desc = "🔇 Left voice", RED, f"{member.mention} left {before.channel.mention}"
            else:
                title, color = "🔀 Moved voice", ORANGE
                desc = f"{member.mention} moved {before.channel.mention} → {after.channel.mention}"
            await self.log(guild, "voice", make(title, color, desc, footer=f"User ID: {member.id}"))
        if before.mute != after.mute:
            await self.log(guild, "voice", make(
                "🎙️ Server mute", ORANGE,
                f"{member.mention} was {'server muted' if after.mute else 'server unmuted'}.",
                footer=f"User ID: {member.id}"))
        if before.deaf != after.deaf:
            await self.log(guild, "voice", make(
                "🎧 Server deafen", ORANGE,
                f"{member.mention} was {'server deafened' if after.deaf else 'server undeafened'}.",
                footer=f"User ID: {member.id}"))

    # ======================================================================
    # Channel events
    # ======================================================================
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        who = await self.executor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        await self.log(channel.guild, "channel", make(
            "📁 Channel created", GREEN, f"{channel.mention} (`{channel.name}`)",
            [("Type", channel.type.name.replace("_", " "), True)] + self.by(who),
            footer=f"Channel ID: {channel.id}"))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        who = await self.executor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        await self.log(channel.guild, "channel", make(
            "🗑️ Channel deleted", RED, f"`{channel.name}`",
            [("Type", channel.type.name.replace("_", " "), True)] + self.by(who),
            footer=f"Channel ID: {channel.id}"))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append(("Topic", f"{cut(before.topic, 400) or '—'} → {cut(after.topic, 400) or '—'}", False))
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(("Slowmode", f"{before.slowmode_delay}s → {after.slowmode_delay}s", True))
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(("NSFW", f"{before.nsfw} → {after.nsfw}", True))
        if before.category != after.category:
            changes.append(("Category", f"{before.category} → {after.category}", True))
        if before.overwrites != after.overwrites:
            changes.append(("Permissions", "Permission overwrites changed", False))
        if not changes:
            return
        who = await self.executor(after.guild, discord.AuditLogAction.channel_update, after.id)
        await self.log(after.guild, "channel", make(
            "📝 Channel updated", ORANGE, after.mention, changes + self.by(who),
            footer=f"Channel ID: {after.id}"))

    # ======================================================================
    # Role events
    # ======================================================================
    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        who = await self.executor(role.guild, discord.AuditLogAction.role_create, role.id)
        await self.log(role.guild, "role", make(
            "🏷️ Role created", GREEN, f"{role.mention} (`{role.name}`)", self.by(who),
            footer=f"Role ID: {role.id}"))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        who = await self.executor(role.guild, discord.AuditLogAction.role_delete, role.id)
        await self.log(role.guild, "role", make(
            "🗑️ Role deleted", RED, f"`{role.name}`", self.by(who), footer=f"Role ID: {role.id}"))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.color != after.color:
            changes.append(("Color", f"{before.color} → {after.color}", True))
        if before.hoist != after.hoist:
            changes.append(("Hoisted", f"{before.hoist} → {after.hoist}", True))
        if before.mentionable != after.mentionable:
            changes.append(("Mentionable", f"{before.mentionable} → {after.mentionable}", True))
        if before.permissions != after.permissions:
            added = [n for n, v in after.permissions if v and not getattr(before.permissions, n)]
            removed = [n for n, v in before.permissions if v and not getattr(after.permissions, n)]
            if added:
                changes.append(("Permissions added", ", ".join(added[:12]), False))
            if removed:
                changes.append(("Permissions removed", ", ".join(removed[:12]), False))
        if not changes:
            return
        who = await self.executor(after.guild, discord.AuditLogAction.role_update, after.id)
        await self.log(after.guild, "role", make(
            "📝 Role updated", ORANGE, after.mention, changes + self.by(who),
            footer=f"Role ID: {after.id}"))

    # ======================================================================
    # Server events
    # ======================================================================
    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.icon != after.icon:
            changes.append(("Icon", "Server icon changed", False))
        if before.banner != after.banner:
            changes.append(("Banner", "Server banner changed", False))
        if before.owner_id != after.owner_id:
            changes.append(("Owner", f"<@{before.owner_id}> → <@{after.owner_id}>", False))
        if before.verification_level != after.verification_level:
            changes.append(("Verification", f"{before.verification_level.name} → {after.verification_level.name}", True))
        if before.afk_channel != after.afk_channel:
            changes.append(("AFK channel", f"{before.afk_channel} → {after.afk_channel}", True))
        if not changes:
            return
        who = await self.executor(after, discord.AuditLogAction.guild_update)
        await self.log(after, "server", make("⚙️ Server updated", ORANGE, None, changes + self.by(who)))

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        old, new = {e.id: e for e in before}, {e.id: e for e in after}
        added = [str(new[i]) for i in new if i not in old]
        removed = [f"`{old[i].name}`" for i in old if i not in new]
        fields = []
        if added:
            fields.append(("Added", " ".join(added), False))
        if removed:
            fields.append(("Removed", " ".join(removed), False))
        if fields:
            await self.log(guild, "server", make("😀 Emojis updated", ORANGE, None, fields))

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        if not invite.guild:
            return
        await self.log(invite.guild, "server", make(
            "🔗 Invite created", GREEN, f"`{invite.code}` → {invite.channel.mention if invite.channel else ''}",
            [("By", invite.inviter.mention if invite.inviter else "Unknown", True),
             ("Max uses", invite.max_uses or "∞", True)]))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        if not invite.guild:
            return
        await self.log(invite.guild, "server", make("🔗 Invite deleted", RED, f"`{invite.code}`"))

    # ======================================================================
    # Member events
    # ======================================================================
    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self.log(member.guild, "member", make(
            "📥 Member joined", GREEN, f"{member.mention} (`{member}`)",
            [("Account created", discord.utils.format_dt(member.created_at, "R"), True),
             ("Member count", member.guild.member_count, True)],
            footer=f"User ID: {member.id}"))

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        who = await self.executor(member.guild, discord.AuditLogAction.kick, member.id)
        title = "👢 Member kicked" if who else "📤 Member left"
        roles = ", ".join(r.mention for r in member.roles if not r.is_default()) or "None"
        await self.log(member.guild, "member", make(
            title, RED, f"{member.mention} (`{member}`)",
            [("Roles", roles, False)] + self.by(who), footer=f"User ID: {member.id}"))

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        who = await self.executor(guild, discord.AuditLogAction.ban, user.id)
        await self.log(guild, "member", make(
            "🔨 Member banned", RED, f"{user.mention} (`{user}`)", self.by(who),
            footer=f"User ID: {user.id}"))

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        who = await self.executor(guild, discord.AuditLogAction.unban, user.id)
        await self.log(guild, "member", make(
            "♻️ Member unbanned", GREEN, f"{user.mention} (`{user}`)", self.by(who),
            footer=f"User ID: {user.id}"))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        guild = after.guild
        # nickname / timeout -> member category
        if before.nick != after.nick:
            await self.log(guild, "member", make(
                "📝 Nickname changed", ORANGE, after.mention,
                [("Before", before.nick or "*none*", True), ("After", after.nick or "*none*", True)],
                footer=f"User ID: {after.id}"))
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                desc = f"{after.mention} timed out until {discord.utils.format_dt(after.timed_out_until, 'f')}"
                title, color = "⏳ Member timed out", RED
            else:
                desc, title, color = f"{after.mention}'s timeout was removed", "✅ Timeout removed", GREEN
            await self.log(guild, "member", make(title, color, desc, footer=f"User ID: {after.id}"))

        # roles added / removed -> role category
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            who = await self.executor(guild, discord.AuditLogAction.member_role_update, after.id)
            fields = []
            if added:
                fields.append(("Added", ", ".join(r.mention for r in added), False))
            if removed:
                fields.append(("Removed", ", ".join(r.mention for r in removed), False))
            await self.log(guild, "role", make(
                "🏷️ Member roles changed", ORANGE, after.mention, fields + self.by(who),
                footer=f"User ID: {after.id}"))


async def setup(bot):
    cog = EventLogging(bot)
    # Skip any command another cog already registered, so we never clash.
    kept, skipped = [], []
    for cmd in cog.__cog_commands__:
        if any(bot.get_command(n) for n in (cmd.name, *cmd.aliases)):
            skipped.append(cmd.name)
        else:
            kept.append(cmd)
    cog.__cog_commands__ = tuple(kept)
    if skipped:
        print(f"event_logging: skipped (already exist): {', '.join(skipped)}")
    await bot.add_cog(cog)
