"""Server backups: save a server's roles, categories and channels, then load them back later.

  backup add <name>      save this server and get a code like 1a4b-2ca1
  backup load <code>     restore a backup here (wipes this server's channels and roles first)
  backup list            show your saved backups
  backup delete <code>   delete a saved backup

Who can use it: the server owner, the bot owner, or an Anti-Nuke admin.
Saved to data/backups.json. A backup holds roles (name, colour, permissions, order), categories,
text / voice / stage / announcement / forum channels, and every permission overwrite.
It does NOT hold messages, members, emojis or webhooks.
"""
import asyncio
import os
import re
import secrets
import time
from typing import Optional

import discord
from discord.ext import commands

from utils.base import COLOR, NO_PINGS, BaseCog
from utils.store import GuildStore

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "482506940027502597"))
KEY = "shared"  # one list of backups for the whole bot
MAX_PER_SERVER = 25  # saved backups per server
CONFIRM_SECONDS = 30
SUPPORTED = ("text", "news", "voice", "stage_voice", "forum")


def normalize_code(text: str) -> Optional[str]:
    """'1A4B2CA1', '1a4b-2ca1' or ' 1a4b 2ca1 ' -> '1a4b-2ca1'. None if it isn't a valid code."""
    raw = re.sub(r"[\s-]", "", text.lower())
    if not re.fullmatch(r"[0-9a-f]{8}", raw):
        return None
    return f"{raw[:4]}-{raw[4:]}"


# ---- reading a server ------------------------------------------------------
def dump_overwrites(channel) -> list:
    out = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        out.append(
            {
                "type": "role" if isinstance(target, discord.Role) else "member",
                "id": target.id,
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return out


def snapshot(guild: discord.Guild) -> dict:
    roles = [
        {
            "id": r.id,
            "name": r.name,
            "color": r.colour.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "permissions": r.permissions.value,
            "position": r.position,
        }
        for r in sorted(guild.roles, key=lambda r: r.position)
        if not r.is_default() and not r.managed
    ]

    categories = [
        {
            "id": c.id,
            "name": c.name,
            "position": c.position,
            "overwrites": dump_overwrites(c),
        }
        for c in sorted(guild.categories, key=lambda c: (c.position, c.id))
    ]

    channels = []
    for ch in sorted(guild.channels, key=lambda c: (c.position, c.id)):
        if isinstance(ch, discord.CategoryChannel) or ch.type.name not in SUPPORTED:
            continue
        channels.append(
            {
                "id": ch.id,
                "name": ch.name,
                "type": ch.type.name,
                "position": ch.position,
                "category_id": ch.category_id,
                "overwrites": dump_overwrites(ch),
                "topic": getattr(ch, "topic", None),
                "nsfw": getattr(ch, "nsfw", False),
                "slowmode_delay": getattr(ch, "slowmode_delay", 0) or 0,
                "bitrate": getattr(ch, "bitrate", None),
                "user_limit": getattr(ch, "user_limit", 0) or 0,
            }
        )

    return {
        "guild_id": guild.id,
        "everyone_permissions": guild.default_role.permissions.value,
        "roles": roles,
        "categories": categories,
        "channels": channels,
    }


# ---- writing a server ------------------------------------------------------
def build_overwrites(guild: discord.Guild, saved: list, role_map: dict, old_guild_id: int) -> dict:
    result = {}
    for ow in saved:
        if ow["type"] == "role":
            if ow["id"] == old_guild_id:
                target = guild.default_role
            else:
                # a role we just recreated, or one that survived the wipe (e.g. a bot's own role)
                target = role_map.get(ow["id"]) or guild.get_role(ow["id"])
        else:
            target = guild.get_member(ow["id"])
        if target is None:
            continue
        result[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
        )
    return result


class Backup(BaseCog, name="Backup"):
    """Save and restore a server's roles, categories and channels"""

    help_text = (
        "# Backup\n\n"
        "**Required:** Server Owner or Anti-Nuke admin\n\n"
        "Saves every role, category and channel (with their permissions) under a short code, "
        "so the server can be rebuilt later. Messages, members, emojis and webhooks are not saved.\n\n"
        "## Commands\n\n"
        "`{p}backup add <name>` - Save this server and get a backup code like `1a4b-2ca1`\n"
        "`{p}backup load <code>` - Restore a backup. **This deletes every channel and role first**, "
        "and asks you to type `yes` to confirm\n"
        "`{p}backup list` - Show your saved backups\n"
        "`{p}backup delete <code>` - Delete a saved backup\n\n"
        "## Tips\n\n"
        "Keep my role at the top of the role list and give me **Administrator** so I can recreate "
        "everything exactly. Roles above my role can't be restored.\n"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GuildStore("backups", {"backups": {}})
        self.loading: set = set()  # server ids with a load running

    # ---- helpers -----------------------------------------------------------
    @property
    def backups(self) -> dict:
        return self.store.get(KEY)["backups"]

    def allowed(self, ctx: commands.Context) -> bool:
        if ctx.author.id in (BOT_OWNER_ID, ctx.guild.owner_id):
            return True
        antinuke = self.bot.get_cog("AntiNuke")
        return bool(antinuke and ctx.author.id in antinuke.cfg(ctx.guild)["admins"])

    async def deny(self, ctx: commands.Context):
        await ctx.send("Only the server owner or an Anti-Nuke admin can use that.")

    def new_code(self) -> str:
        while True:
            code = f"{secrets.token_hex(2)}-{secrets.token_hex(2)}"
            if code not in self.backups:
                return code

    # ---- commands ----------------------------------------------------------
    @commands.group(name="backup", invoke_without_command=True)
    @commands.guild_only()
    async def backup(self, ctx: commands.Context):
        """Save and restore this server's roles, categories and channels."""
        p = ctx.clean_prefix
        await ctx.send(
            f"`{p}backup add <name>` - save this server\n"
            f"`{p}backup load <code>` - restore a backup\n"
            f"`{p}backup list` - show saved backups\n"
            f"`{p}backup delete <code>` - delete a backup"
        )

    @backup.command(name="add", aliases=["create"])
    async def backup_add(self, ctx: commands.Context, *, name: str):
        """Save this server's roles, categories and channels and get a backup code."""
        if not self.allowed(ctx):
            return await self.deny(ctx)

        mine = sum(1 for b in self.backups.values() if b["guild_id"] == ctx.guild.id)
        if mine >= MAX_PER_SERVER:
            return await ctx.send(
                f"This server already has {MAX_PER_SERVER} backups. "
                f"Remove one with `{ctx.clean_prefix}backup delete <code>` first."
            )

        data = snapshot(ctx.guild)
        code = self.new_code()
        self.backups[code] = {
            "name": name.strip()[:50],
            "guild_id": ctx.guild.id,
            "guild_name": ctx.guild.name,
            "created_by": ctx.author.id,
            "created_at": int(time.time()),
            "data": data,
        }
        self.store.save()
        await ctx.send(f"Backup success `{code}`")

    @backup.command(name="list", aliases=["ls"])
    async def backup_list(self, ctx: commands.Context):
        """Show your saved backups."""
        if not self.allowed(ctx):
            return await self.deny(ctx)

        found = [
            (code, b)
            for code, b in self.backups.items()
            if b["guild_id"] == ctx.guild.id or b["created_by"] == ctx.author.id
        ]
        if not found:
            return await ctx.send(
                f"No backups saved yet. Make one with `{ctx.clean_prefix}backup add <name>`."
            )

        found.sort(key=lambda item: item[1]["created_at"], reverse=True)
        lines = []
        for code, b in found[:25]:
            d = b["data"]
            line = (
                f"`{code}` **{b['name']}** - {len(d['roles'])} roles, "
                f"{len(d['categories'])} categories, {len(d['channels'])} channels - "
                f"<t:{b['created_at']}:R>"
            )
            if b["guild_id"] != ctx.guild.id:
                line += f" (from {b['guild_name']})"
            lines.append(line)

        embed = discord.Embed(title="Saved backups", description="\n".join(lines), color=COLOR)
        if len(found) > 25:
            embed.set_footer(text=f"Showing 25 of {len(found)}")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @backup.command(name="delete", aliases=["remove", "del"])
    async def backup_delete(self, ctx: commands.Context, *, code: str):
        """Delete a saved backup."""
        if not self.allowed(ctx):
            return await self.deny(ctx)

        key = normalize_code(code)
        entry = self.backups.get(key) if key else None
        if entry is None:
            return await ctx.send("I couldn't find a backup with that code.")
        if ctx.author.id != BOT_OWNER_ID and entry["guild_id"] != ctx.guild.id \
                and entry["created_by"] != ctx.author.id:
            return await ctx.send("That backup doesn't belong to this server or to you.")

        del self.backups[key]
        self.store.save()
        await ctx.send(f"Deleted backup `{key}`.")

    @backup.command(name="load", aliases=["restore"])
    async def backup_load(self, ctx: commands.Context, *, code: str):
        """Restore a backup here. Deletes every channel and role first."""
        if not self.allowed(ctx):
            return await self.deny(ctx)

        key = normalize_code(code)
        entry = self.backups.get(key) if key else None
        if entry is None:
            return await ctx.send("I couldn't find a backup with that code.")

        guild = ctx.guild
        perms = guild.me.guild_permissions
        if not (perms.administrator or (perms.manage_channels and perms.manage_roles)):
            return await ctx.send("I need the **Manage Channels** and **Manage Roles** permissions to do that.")
        if guild.id in self.loading:
            return await ctx.send("A backup is already loading in this server.")

        await ctx.send(
            f"{ctx.author.mention} this will **delete every channel and role** in this server and "
            f"replace them with the backup **{entry['name']}** (`{key}`). "
            f"Type `yes` to continue or `cancel` to stop.",
            allowed_mentions=discord.AllowedMentions(users=[ctx.author]),
        )

        def check(m: discord.Message) -> bool:
            return (
                m.author.id == ctx.author.id
                and m.channel.id == ctx.channel.id
                and m.content.strip().lower() in ("yes", "cancel")
            )

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=CONFIRM_SECONDS)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out, nothing was changed.")
        if reply.content.strip().lower() == "cancel":
            return await ctx.send("Cancelled, nothing was changed.")

        self.loading.add(guild.id)
        try:
            await ctx.send("Loading the backup, this can take a few minutes...")
            first_text, skipped = await self.restore(guild, entry["data"], f"Backup {key} loaded by {ctx.author}")
        finally:
            self.loading.discard(guild.id)

        message = "Backup loaded"
        if skipped:
            message += f" ({skipped} things couldn't be restored)"
        # the channel the command was typed in is gone, so post in the first new text channel
        # (or DM the author if no text channel could be made)
        try:
            if first_text is not None:
                await first_text.send(message)
            else:
                await ctx.author.send(message)
        except discord.HTTPException:
            pass

    # ---- the actual restore ------------------------------------------------
    async def restore(self, guild: discord.Guild, data: dict, reason: str):
        """Wipe the server, then rebuild it from a snapshot. Returns (first text channel, skipped count)."""
        skipped = 0
        me = guild.me
        is_admin = me.guild_permissions.administrator

        # 1. wipe every channel, then every role the bot is allowed to delete
        for channel in list(guild.channels):
            try:
                await channel.delete(reason=reason)
            except discord.HTTPException:
                skipped += 1  # e.g. the rules / updates channels of a Community server
        for role in list(guild.roles):
            if role.is_default() or role.managed or role >= me.top_role:
                continue
            try:
                await role.delete(reason=reason)
            except discord.HTTPException:
                skipped += 1

        # 2. @everyone's permissions
        try:
            await guild.default_role.edit(
                permissions=discord.Permissions(data["everyone_permissions"]), reason=reason
            )
        except discord.HTTPException:
            skipped += 1

        # 3. roles: highest first, because Discord puts each new role at the bottom
        role_map = {}
        saved_roles = sorted(data["roles"], key=lambda r: r["position"])
        for r in reversed(saved_roles):
            value = r["permissions"]
            if not is_admin:  # a bot can't hand out permissions it doesn't have
                value &= me.guild_permissions.value
            try:
                role_map[r["id"]] = await guild.create_role(
                    name=r["name"],
                    permissions=discord.Permissions(value),
                    colour=discord.Colour(r["color"]),
                    hoist=r["hoist"],
                    mentionable=r["mentionable"],
                    reason=reason,
                )
            except discord.HTTPException:
                skipped += 1

        # put them in the saved order, if they fit under the bot's own role
        ordered = [role_map[r["id"]] for r in saved_roles if r["id"] in role_map]
        if ordered and len(ordered) < me.top_role.position:
            try:
                await guild.edit_role_positions(
                    positions={role: i for i, role in enumerate(ordered, start=1)}, reason=reason
                )
            except discord.HTTPException:
                pass

        # 4. categories
        old_guild_id = data["guild_id"]
        category_map = {}
        for c in data["categories"]:
            overwrites = build_overwrites(guild, c["overwrites"], role_map, old_guild_id)
            try:
                try:
                    category_map[c["id"]] = await guild.create_category(
                        c["name"], overwrites=overwrites, reason=reason
                    )
                except discord.Forbidden:
                    category_map[c["id"]] = await guild.create_category(c["name"], reason=reason)
            except discord.HTTPException:
                skipped += 1

        # 5. channels
        first_text = None
        for ch in data["channels"]:
            overwrites = build_overwrites(guild, ch["overwrites"], role_map, old_guild_id)
            category = category_map.get(ch["category_id"])
            try:
                try:
                    created = await self.make_channel(guild, ch, category, overwrites, reason)
                except discord.Forbidden:
                    # usually "can't set a permission I don't have", so try again without overwrites
                    created = await self.make_channel(guild, ch, category, {}, reason)
            except discord.HTTPException:
                skipped += 1
                continue
            if first_text is None and isinstance(created, discord.TextChannel):
                first_text = created

        return first_text, skipped

    async def make_channel(self, guild, ch: dict, category, overwrites: dict, reason: str):
        kind = ch["type"]
        base = {"name": ch["name"], "overwrites": overwrites, "reason": reason}
        if category is not None:
            base["category"] = category

        if kind in ("text", "news"):
            text = dict(base, nsfw=bool(ch["nsfw"]), slowmode_delay=ch["slowmode_delay"])
            if ch["topic"]:
                text["topic"] = ch["topic"]
            if kind == "news":
                try:
                    return await guild.create_text_channel(news=True, **text)
                except discord.HTTPException:
                    pass  # announcement channels need Community, so make a normal text channel
            return await guild.create_text_channel(**text)

        if kind in ("voice", "stage_voice"):
            bitrate = min(ch["bitrate"] or 64000, guild.bitrate_limit)
            if kind == "stage_voice":
                try:
                    return await guild.create_stage_channel(**base)
                except discord.HTTPException:
                    pass  # stages need Community, so fall back to a voice channel
            return await guild.create_voice_channel(
                bitrate=bitrate, user_limit=ch["user_limit"], **base
            )

        # forum
        forum = dict(base, nsfw=bool(ch["nsfw"]), slowmode_delay=ch["slowmode_delay"])
        if ch["topic"]:
            forum["topic"] = ch["topic"]
        try:
            return await guild.create_forum(**forum)
        except discord.HTTPException:
            return await guild.create_text_channel(**base)  # forums need Community


async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))