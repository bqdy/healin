"""
Community Utilities (single file)

xp (rank, top, card, config, reward, rewards, remove), stickymsg, unstickymsg, stickylist.

Standalone: needs discord.py and aiosqlite. Pillow is optional (rank card image;
without it, -xp rank falls back to an embed).
"""
import asyncio
import io
import json
import random
import re
import time
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands, tasks

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None


# ======================================================================
# Helpers (storage + embed script parser)
# ======================================================================
class Store:
    """Tiny per-guild JSON key/value store on top of SQLite."""

    def __init__(self, path="community_utilities.db"):
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

    async def delete(self, guild_id, key):
        await self.db.execute("DELETE FROM kv WHERE guild_id=? AND key=?", (guild_id, key))
        await self.db.commit()

    async def prefix(self, guild_id, prefix):
        """All keys in a guild starting with prefix -> {key: value}."""
        async with self.db.execute(
            "SELECT key, value FROM kv WHERE guild_id=? AND key LIKE ?",
            (guild_id, prefix + "%"),
        ) as cur:
            rows = await cur.fetchall()
        return {k: json.loads(v) for k, v in rows}

    async def all(self, key):
        """Same key across every guild -> [(guild_id, value)]."""
        async with self.db.execute("SELECT guild_id, value FROM kv WHERE key=?", (key,)) as cur:
            rows = await cur.fetchall()
        return [(g, json.loads(v)) for g, v in rows]


def fmt(text, member=None, guild=None):
    """Replace variables like {user.mention}, {server}, {count}."""
    guild = guild or (member.guild if member else None)
    rep = {}
    if member:
        rep.update({
            "{user}": str(member),
            "{user.name}": member.display_name,
            "{user.mention}": member.mention,
            "{user.id}": str(member.id),
        })
    if guild:
        rep.update({
            "{server}": guild.name,
            "{count}": str(guild.member_count),
            "{boosts}": str(guild.premium_subscription_count),
        })
    for k, v in rep.items():
        text = text.replace(k, v)
    return text


def build(text, member=None, guild=None):
    """
    Turn a message or embed script into (content, embed).

    Embed script example:
    {embed}{title: Hello}$v{description: Welcome {user.mention}}$v{color: #5865F2}
    Supported: content, title, description, color, footer, image, thumbnail,
    author, field (name|value|inline true/false)
    """
    text = fmt(text, member, guild)
    if not text.startswith("{embed}"):
        return text, None

    content = None
    emb = discord.Embed()
    for seg in text[len("{embed}"):].split("$v"):
        m = re.match(r"\s*\{(\w+):\s*(.*)\}\s*$", seg, re.S)
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip()
        if k == "content":
            content = v
        elif k == "title":
            emb.title = v
        elif k == "description":
            emb.description = v
        elif k == "color":
            try:
                emb.color = discord.Color(int(v.lstrip("#"), 16))
            except ValueError:
                pass
        elif k == "footer":
            emb.set_footer(text=v)
        elif k == "image":
            emb.set_image(url=v)
        elif k == "thumbnail":
            emb.set_thumbnail(url=v)
        elif k == "author":
            emb.set_author(name=v)
        elif k == "field":
            p = [x.strip() for x in v.split("|")]
            if len(p) >= 2:
                emb.add_field(name=p[0], value=p[1], inline=not (len(p) > 2 and p[2].lower() == "false"))
    return content, emb


async def show_subs(ctx):
    """Called when a group is used with no arguments: list its subcommands."""
    cmd = ctx.command
    lines = [f"`{ctx.clean_prefix}{c.qualified_name} {c.signature}`" for c in cmd.commands]
    embed = discord.Embed(
        title=f"{ctx.clean_prefix}{cmd.qualified_name}",
        description="\n".join(lines) or "No subcommands.",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


# ======================================================================
# Cog
# ======================================================================
def level_from_xp(xp):
    return int((xp / 100) ** 0.5)


def xp_for_level(level):
    return 100 * level * level


def hex_to_rgb(s, default):
    s = (s or "").lstrip("#")
    if len(s) != 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def font(size):
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


class CommunityUtilities(commands.Cog, name="Community Utilities"):
    """XP, rank cards, rewards and sticky messages"""

    help_text = """# Community Utilities

`{p}xp` - Chat and voice XP, rank cards...
`{p}xp rank [@user]` - View a customizable rank card
`{p}xp top` - Top 20 XP rankings
`{p}xp card <accent> [background]` - Personal card colors
`{p}xp config <on/off> [chat] [voice]` - XP settings (Manage Server)
`{p}xp reward <level> @role` / `{p}xp rewards` / `{p}xp remove @role` - Tiered rewards
`{p}stickymsg [#channel] <text or embed script>` - Bottom-of-channel notice
`{p}unstickymsg [#channel]` / `{p}stickylist` - Manage notices

Use each command with no arguments to see its subcommands."""

    def __init__(self, bot):
        self.bot = bot
        self.store = Store()
        self.cooldowns = {}
        self.locks = {}
        self.voice_loop.start()

    async def cog_load(self):
        await self.store.start()

    async def cog_unload(self):
        self.voice_loop.cancel()
        if self.store.db:
            await self.store.db.close()

    # ---------- helpers ----------
    async def cfg(self, gid):
        saved = await self.store.get(gid, "xp:config", {})
        return {"enabled": True, "chat": 20, "voice": 10, **saved}

    async def add_xp(self, member, amount, channel=None):
        gid = member.guild.id
        key = f"xp:user:{member.id}"
        data = await self.store.get(gid, key, {"xp": 0})
        old = level_from_xp(data["xp"])
        data["xp"] += amount
        await self.store.set(gid, key, data)
        new = level_from_xp(data["xp"])
        if new > old:
            await self.level_up(member, new, channel)

    async def level_up(self, member, level, channel):
        rewards = await self.store.get(member.guild.id, "xp:rewards", {})
        for lvl, role_id in rewards.items():
            if int(lvl) <= level:
                role = member.guild.get_role(role_id)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="XP level reward")
                    except discord.HTTPException:
                        pass
        if channel:
            await channel.send(f"🎉 {member.mention} reached **level {level}**!")

    async def leaderboard(self, gid):
        rows = await self.store.prefix(gid, "xp:user:")
        ranking = [(int(k.split(":")[-1]), v) for k, v in rows.items()]
        return sorted(ranking, key=lambda r: r[1]["xp"], reverse=True)

    async def make_card(self, member, data, rank):
        xp = data["xp"]
        level = level_from_xp(xp)
        accent = hex_to_rgb(data.get("accent"), (88, 101, 242))
        bg = hex_to_rgb(data.get("background"), (32, 34, 37))

        img = Image.new("RGB", (800, 240), bg)
        d = ImageDraw.Draw(img)

        avatar_bytes = await member.display_avatar.replace(size=128, format="png").read()
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((140, 140))
        mask = Image.new("L", (140, 140), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 140, 140), fill=255)
        img.paste(avatar, (35, 50), mask)

        base, nxt = xp_for_level(level), xp_for_level(level + 1)
        progress = (xp - base) / (nxt - base)

        d.text((210, 40), member.display_name, fill="white", font=font(38))
        d.text((210, 95), f"Rank #{rank}   Level {level}", fill=accent, font=font(30))
        d.text((210, 130), f"{xp - base} / {nxt - base} XP", fill=(200, 200, 200), font=font(22))
        d.rounded_rectangle((210, 175, 760, 205), radius=15, fill=(70, 72, 77))
        if progress > 0:
            d.rounded_rectangle((210, 175, 210 + int(550 * progress), 205), radius=15, fill=accent)

        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return discord.File(buf, "rank.png")

    # ---------- events ----------
    async def _xp_message(self, message):
        if message.author.bot or not message.guild:
            return
        cfg = await self.cfg(message.guild.id)
        if not cfg["enabled"] or cfg["chat"] <= 0:
            return
        now = time.time()
        key = (message.guild.id, message.author.id)
        if now - self.cooldowns.get(key, 0) < 60:
            return
        self.cooldowns[key] = now
        amount = random.randint(max(1, cfg["chat"] - 5), cfg["chat"] + 5)
        await self.add_xp(message.author, amount, message.channel)

    @tasks.loop(minutes=1)
    async def voice_loop(self):
        for guild in self.bot.guilds:
            cfg = await self.cfg(guild.id)
            if not cfg["enabled"] or cfg["voice"] <= 0:
                continue
            for vc in guild.voice_channels:
                humans = [m for m in vc.members if not m.bot]
                if len(humans) < 2 or vc == guild.afk_channel:
                    continue
                for m in humans:
                    if m.voice and not (m.voice.self_mute or m.voice.self_deaf):
                        await self.add_xp(m, cfg["voice"])

    @voice_loop.before_loop
    async def before_voice(self):
        await self.bot.wait_until_ready()

    # ---------- commands ----------
    @commands.group(name="xp", invoke_without_command=True)
    async def xp_group(self, ctx):
        await show_subs(ctx)

    @xp_group.command(name="rank")
    async def xp_rank(self, ctx, member: discord.Member = None):
        """View a customizable rank card."""
        member = member or ctx.author
        ranking = await self.leaderboard(ctx.guild.id)
        data = await self.store.get(ctx.guild.id, f"xp:user:{member.id}", {"xp": 0})
        pos = next((i for i, (uid, _) in enumerate(ranking, 1) if uid == member.id), len(ranking) + 1)
        if Image is None:
            lvl = level_from_xp(data["xp"])
            return await ctx.send(embed=discord.Embed(
                title=member.display_name,
                description=f"Rank **#{pos}** | Level **{lvl}** | **{data['xp']}** XP",
            ))
        async with ctx.typing():
            file = await self.make_card(member, data, pos)
        await ctx.send(file=file)

    @xp_group.command(name="top")
    async def xp_top(self, ctx):
        """Top 20 XP rankings."""
        ranking = (await self.leaderboard(ctx.guild.id))[:20]
        if not ranking:
            return await ctx.send("Nobody has any XP yet.")
        lines = [
            f"**{i}.** <@{uid}> — Level {level_from_xp(d['xp'])} ({d['xp']} XP)"
            for i, (uid, d) in enumerate(ranking, 1)
        ]
        await ctx.send(embed=discord.Embed(
            title=f"🏆 {ctx.guild.name} Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        ))

    @xp_group.command(name="card")
    async def xp_card(self, ctx, accent: str, background: str = None):
        """Personal card colors (hex, e.g. #ff0000)."""
        if len(accent.lstrip("#")) != 6 or (background and len(background.lstrip("#")) != 6):
            return await ctx.send("Use 6-digit hex colors, e.g. `-xp card #ff5555 #1e1e2e`.")
        key = f"xp:user:{ctx.author.id}"
        data = await self.store.get(ctx.guild.id, key, {"xp": 0})
        data["accent"] = accent
        if background:
            data["background"] = background
        await self.store.set(ctx.guild.id, key, data)
        await ctx.send("✅ Card colors updated. Check it with `-xp rank`.")

    @xp_group.command(name="config")
    @commands.has_permissions(manage_guild=True)
    async def xp_config(self, ctx, toggle: str, chat: int = None, voice: int = None):
        """XP settings: on/off, XP per message, XP per voice minute."""
        if toggle.lower() not in ("on", "off"):
            return await ctx.send("First argument must be `on` or `off`.")
        cfg = await self.cfg(ctx.guild.id)
        cfg["enabled"] = toggle.lower() == "on"
        if chat is not None:
            cfg["chat"] = chat
        if voice is not None:
            cfg["voice"] = voice
        await self.store.set(ctx.guild.id, "xp:config", cfg)
        await ctx.send(
            f"✅ XP **{'enabled' if cfg['enabled'] else 'disabled'}** | "
            f"chat: {cfg['chat']} XP/msg | voice: {cfg['voice']} XP/min"
        )

    @xp_group.command(name="reward")
    @commands.has_permissions(manage_roles=True)
    async def xp_reward(self, ctx, level: int, role: discord.Role):
        """Give a role at a level."""
        rewards = await self.store.get(ctx.guild.id, "xp:rewards", {})
        rewards[str(level)] = role.id
        await self.store.set(ctx.guild.id, "xp:rewards", rewards)
        await ctx.send(f"✅ {role.mention} will be given at level **{level}**.",
                       allowed_mentions=discord.AllowedMentions.none())

    @xp_group.command(name="rewards")
    async def xp_rewards(self, ctx):
        """List tiered rewards."""
        rewards = await self.store.get(ctx.guild.id, "xp:rewards", {})
        if not rewards:
            return await ctx.send("No rewards set.")
        lines = [f"Level **{lvl}** → <@&{rid}>" for lvl, rid in sorted(rewards.items(), key=lambda x: int(x[0]))]
        await ctx.send(embed=discord.Embed(title="XP Rewards", description="\n".join(lines)))

    @xp_group.command(name="remove")
    @commands.has_permissions(manage_roles=True)
    async def xp_remove(self, ctx, role: discord.Role):
        """Remove a reward role."""
        rewards = await self.store.get(ctx.guild.id, "xp:rewards", {})
        rewards = {k: v for k, v in rewards.items() if v != role.id}
        await self.store.set(ctx.guild.id, "xp:rewards", rewards)
        await ctx.send("✅ Reward removed.")

    # ---------- shared listener ----------
    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild:
            return
        await self._xp_message(message)
        if message.author.id != self.bot.user.id:
            if await self.store.get(message.guild.id, f"sticky:{message.channel.id}"):
                await self.repost(message.channel)

    # ---------- sticky messages ----------
    async def repost(self, channel, new_data=None):
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            key = f"sticky:{channel.id}"
            data = new_data or await self.store.get(channel.guild.id, key)
            if not data:
                return
            if data.get("last"):
                try:
                    await channel.get_partial_message(data["last"]).delete()
                except discord.HTTPException:
                    pass
            content, embed = build(data["text"], guild=channel.guild)
            msg = await channel.send(content=content, embed=embed)
            data["last"] = msg.id
            await self.store.set(channel.guild.id, key, data)

    @commands.command(name="stickymsg")
    @commands.has_permissions(manage_messages=True)
    async def stickymsg(self, ctx, channel: Optional[discord.TextChannel] = None, *, text: str):
        """Bottom-of-channel notice. Supports plain text or {embed} scripts."""
        channel = channel or ctx.channel
        await self.repost(channel, {"text": text, "last": None})
        await ctx.message.add_reaction("✅")

    @commands.command(name="unstickymsg")
    @commands.has_permissions(manage_messages=True)
    async def unstickymsg(self, ctx, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        key = f"sticky:{channel.id}"
        data = await self.store.get(ctx.guild.id, key)
        if not data:
            return await ctx.send("No sticky message in that channel.")
        if data.get("last"):
            try:
                await channel.get_partial_message(data["last"]).delete()
            except discord.HTTPException:
                pass
        await self.store.delete(ctx.guild.id, key)
        await ctx.send(f"✅ Removed sticky from {channel.mention}.")

    @commands.command(name="stickylist")
    async def stickylist(self, ctx):
        rows = await self.store.prefix(ctx.guild.id, "sticky:")
        if not rows:
            return await ctx.send("No sticky messages.")
        lines = [f"<#{k.split(':')[1]}> — {v['text'][:60]}" for k, v in rows.items()]
        await ctx.send(embed=discord.Embed(title="Sticky messages", description="\n".join(lines)))


async def setup(bot):
    cog = CommunityUtilities(bot)
    kept, skipped = [], []
    for cmd in cog.__cog_commands__:
        if any(bot.get_command(n) for n in (cmd.name, *cmd.aliases)):
            skipped.append(cmd.name)
        else:
            kept.append(cmd)
    cog.__cog_commands__ = tuple(kept)
    if skipped:
        print(f"community_utilities: skipped (already exist): {', '.join(skipped)}")
    await bot.add_cog(cog)
