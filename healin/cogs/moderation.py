"""
Moderation cog for discord.py 2.4+   ->   save as  cogs/moderation.py

Needs:   pip install -U discord.py aiohttp
Intents: Server Members + Message Content
Data:    moderation_data.json (created next to the bot)

Notes
- kick, ban, hardban and timeout have NO aliases.
- Aliases that are already used by another cog are removed automatically at load,
  so this cog can't break other cogs.
- Commands whose *name* already exists in another cog are skipped (silently).
- `alias`, `aa`, `ar` are left to your cogs.aliases cog.
- Bot owners (OWNER_IDS in .env / bot.owner_ids) skip permission and role-hierarchy checks.
"""
import asyncio
import io
import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict, deque
from datetime import timedelta
from typing import Optional, Union

import aiohttp
import discord
from discord.ext import commands, tasks

from utils.punish import dm_punishment, notify

DATA_FILE = "moderation_data.json"

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
db = {"warns": {}, "hardbans": {}, "jails": {}, "blist": {}, "timers": [], "cases": {},
      "notes": {}, "saved_roles": {}, "config": {}, "reminders": [], "filters": {},
      "disabled": {}, "ignores": {}, "restrict": {}, "counters": {}, "autoroles": {},
      "antiraid": {}, "fakeperms": {}, "snipe_filters": {}, "legacy_words": {}}
if os.path.exists(DATA_FILE):
    try:
        with open(DATA_FILE) as f:
            db.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        print("[moderation] could not read moderation_data.json, starting fresh")


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, DATA_FILE)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class Blocked(commands.CheckFailure):
    pass


def is_bot_owner(ctx):
    """True for the bot owners (OWNER_IDS in .env, plus the Developer Portal application owner)."""
    return ctx.author.id in (getattr(ctx.bot, "owner_ids", None) or ())


async def send(ctx, content=None, **kwargs):
    """ctx.send that never pings roles/users/everyone."""
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    return await ctx.send(content, **kwargs)


def fake_perms_for(member):
    conf = db["fakeperms"].get(str(member.guild.id), {})
    perms = set(conf.get(f"user:{member.id}", []))
    for r in member.roles:
        perms.update(conf.get(f"role:{r.id}", []))
    return perms


def has_perms(**needed):
    """Like commands.has_permissions, but also honours /fp virtual permissions."""
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if is_bot_owner(ctx):
            return True
        real = ctx.channel.permissions_for(ctx.author)
        fake = fake_perms_for(ctx.author) if isinstance(ctx.author, discord.Member) else set()
        if "administrator" in fake:
            return True
        missing = [p for p, v in needed.items() if v and not getattr(real, p, False) and p not in fake]
        if missing:
            raise commands.MissingPermissions(missing)
        return True
    return commands.check(predicate)


def owner_or_admin():
    async def predicate(ctx):
        if is_bot_owner(ctx):
            return True
        if ctx.guild and (ctx.author == ctx.guild.owner or ctx.author.guild_permissions.administrator):
            return True
        raise commands.MissingPermissions(["administrator"])
    return commands.check(predicate)


def owner_only():
    async def predicate(ctx):
        if is_bot_owner(ctx):
            return True
        if ctx.guild and ctx.author == ctx.guild.owner:
            return True
        raise commands.MissingPermissions(["server owner"])
    return commands.check(predicate)


# --------------------------------------------------------------------------
# Nuke (single channel)
# --------------------------------------------------------------------------
def nuke_allowed():
    """Server owner, Anti-Nuke admin, or anyone with the Administrator permission."""
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        member = ctx.author
        if member == ctx.guild.owner or member.guild_permissions.administrator:
            return True
        antinuke = ctx.bot.get_cog("AntiNuke")
        if antinuke and member.id in antinuke.cfg(ctx.guild)["admins"]:
            return True
        raise Blocked("Only the server owner, Anti-Nuke admins or members with "
                      "Administrator can use `nuke`.")
    return commands.check(predicate)


UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text) -> Optional[int]:
    m = re.fullmatch(r"(\d+)([smhdw])", (text or "").lower())
    return int(m[1]) * UNITS[m[2]] if m else None


def split_duration(text):
    """'10m being rude' -> (600, '10m', 'being rude'); no duration -> (None, None, text)"""
    parts = text.split(maxsplit=1) if text else []
    if parts:
        secs = parse_duration(parts[0])
        if secs:
            return secs, parts[0], (parts[1] if len(parts) > 1 else "")
    return None, None, text


def why(ctx, reason):
    return f"{ctx.author} ({ctx.author.id}): {reason or 'No reason provided'}"[:512]


COLORS = {
    "red": 0xE74C3C, "blue": 0x3498DB, "green": 0x2ECC71, "orange": 0xE67E22,
    "purple": 0x9B59B6, "gold": 0xF1C40F, "teal": 0x1ABC9C, "magenta": 0xE91E63,
    "blurple": 0x5865F2, "white": 0xFFFFFF, "black": 0x010101, "pink": 0xFF69B4,
}


def parse_color(text) -> Optional[int]:
    text = text.strip().lower()
    if re.fullmatch(r"#?[0-9a-f]{6}", text):
        return int(text.lstrip("#"), 16)
    return COLORS.get(text)


def add_timer(kind, guild_id, user_id, seconds, role_id=None):
    db["timers"].append({"kind": kind, "guild": guild_id, "user": user_id,
                         "role": role_id, "at": time.time() + seconds})
    save()


def remove_timers(kind, guild_id, user_id, role_id=None):
    db["timers"] = [
        t for t in db["timers"]
        if not (t["kind"] == kind and t["guild"] == guild_id and t["user"] == user_id
                and (role_id is None or t["role"] == role_id))
    ]
    save()


def is_all(arg):
    return bool(arg) and arg.lower() == "all"


def parse_on(text):
    t = (text or "").lower()
    if t in ("on", "true", "enable", "yes"):
        return True
    if t in ("off", "false", "disable", "no"):
        return False
    raise commands.BadArgument("Use on or off.")


STAFF_PERMS = ("administrator", "kick_members", "ban_members", "manage_guild", "manage_roles",
               "manage_channels", "manage_messages", "moderate_members", "manage_nicknames",
               "mention_everyone", "manage_threads")

MUTE_ROLES = {
    "Muted": dict(send_messages=False, add_reactions=False, speak=False, create_public_threads=False,
                  create_private_threads=False, send_messages_in_threads=False),
    "Image Muted": dict(attach_files=False, embed_links=False),
    "Reaction Muted": dict(add_reactions=False, use_external_emojis=False, use_external_stickers=False),
}

FILTERS = {
    "bots": lambda m: m.author.bot,
    "humans": lambda m: not m.author.bot,
    "images": lambda m: any((a.content_type or "").startswith("image/") for a in m.attachments)
    or any(e.type in ("image", "gifv") for e in m.embeds)
    or bool(re.search(r"\.(png|jpe?g|gif|webp)(\?|$)|tenor\.com|giphy\.com", m.content, re.I)),
    "files": lambda m: bool(m.attachments),
    "emotes": lambda m: bool(re.search(r"<a?:\w+:\d+>", m.content)),
    "links": lambda m: bool(re.search(r"https?://", m.content)),
    "invites": lambda m: bool(re.search(r"discord(?:\.gg|(?:app)?\.com/invite)/\w+", m.content)),
}

FILTER_TYPES = ("spam", "links", "caps", "invites")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/\w+", re.I)
RULE_NAME = "Bot Keyword Filter"
SNIPE_TYPES = ("invites", "links", "images", "files", "music")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
AUDIO_EXT = (".mp3", ".wav", ".ogg", ".flac", ".m4a")
MUSIC_RE = re.compile(r"spotify\.com|music\.youtube\.com|soundcloud\.com|music\.apple\.com|deezer\.com|"
                      r"tidal\.com|bandcamp\.com", re.I)
MANAGE_CMDS = {"disablecommand", "enablecommand", "ignore", "restrictcommand"}
VALID_PERMS = {name for name, _ in discord.Permissions.all()}

Target = Union[discord.abc.GuildChannel, discord.Member]


def fmt_case(c):
    dur = f" ({c['duration']})" if c.get("duration") else ""
    return (f"`#{c['id']}` **{c['action']}**{dur} <@{c['user']}> by <@{c['mod']}> "
            f"<t:{c['at']}:d> — {c['reason'][:80]}")


def fp_key(target):
    return f"{'role' if isinstance(target, discord.Role) else 'user'}:{target.id}"


def filt_conf(gid, ftype):
    g = db["filters"].setdefault(str(gid), {})
    return g.setdefault(ftype, {"all": False, "channels": [], "threshold": None,
                                "interval": None, "exempt": []})


def ar_conf(gid):
    return db["antiraid"].setdefault(str(gid), {
        "newaccounts": {"on": False, "days": 7}, "avatar": False,
        "massjoin": {"on": False, "count": 5, "seconds": 10},
        "whitelist": [], "raid_until": 0})


def dis_conf(gid):
    return db["disabled"].setdefault(str(gid), {"all": [], "channels": {}, "members": {}})


def ign_conf(gid):
    return db["ignores"].setdefault(str(gid), {"channels": [], "members": []})


def restrict_conf(gid):
    return db["restrict"].setdefault(str(gid), {})


def split_words(text):
    return [w.strip().lower() for w in text.split(",") if w.strip()]


async def safe_delete(m):
    try:
        await m.delete()
    except discord.HTTPException:
        pass


# ==========================================================================
# THE COG
# ==========================================================================
class Moderation(commands.Cog):
    # Hand-written help pages for the main help menu. A list = several pages (Previous / Next).
    # {p} is replaced with the server's prefix.
    help_text = [
        # ---- page 1
        "# Moderation\n"
        "\n"
        "## Bans\n"
        "**Required:** Ban Members\n"
        "`{p}ban @user [reason]` - Ban a member\n"
        "`{p}tempban @user <duration> [reason]` - Ban for a set time\n"
        "`{p}softban @user [reason]` - Ban then unban to clear their recent messages\n"
        "`{p}hardban @user [reason]` - Ban that is reverted if someone unbans them\n"
        "`{p}hardban list` - View hardbanned users\n"
        "`{p}hardban clear` - Clear the hardban list (Manage Server)\n"
        "`{p}unban @user` - Unban a user (alias `ub`)\n"
        "`{p}unforeverban @user` - Lift a hardban (aliases `unfb`, `unhardban`)\n"
        "`{p}unbanall` - Unban everyone, `{p}unbanall cancel` stops it (Administrator)\n"
        "\n"
        "## Kicks, Timeouts and Warnings\n"
        "`{p}kick @member [reason]` - Kick a member (Kick Members)\n"
        "`{p}timeout @member <duration> [reason]` - Time a member out\n"
        "`{p}untimeout @member [reason]` - Remove a timeout (alias `uto`)\n"
        "`{p}warn @member [reason]` - Warn a member\n"
        "`{p}warnings [@member]` - View warnings\n"
        "`{p}clearwarnings @member` - Clear a member's warnings\n"
        "\n"
        "## Mutes\n"
        "**Required:** Moderate Members\n"
        "`{p}setupmute` - Create the mute roles (Manage Roles)\n"
        "`{p}mute @member [duration] [reason]` - Mute a member (alias `stfu`)\n"
        "`{p}unmute @member [reason]` - Unmute a member\n"
        "`{p}imute @member [duration] [reason]` - Image mute\n"
        "`{p}iunmute @member [reason]` - Remove an image mute\n"
        "`{p}rmute @member [duration] [reason]` - Reaction mute\n"
        "`{p}runmute @member [reason]` - Remove a reaction mute\n"
        "\n"
        "## Jail\n"
        "**Required:** Manage Roles\n"
        "`{p}modsetup` - Create the jail role and channel (Manage Server, alias `setupmod`)\n"
        "`{p}jail @member [duration] [reason]` - Jail a member and strip their roles\n"
        "`{p}unjail @member [reason]` - Release a member\n"
        "`{p}jaillist` - View jailed members\n"
        "`{p}stripstaff @member` - Remove a member's staff roles\n"
        "\n"
        "## Records\n"
        "**Required:** Moderate Members\n"
        "`{p}history [@user] [action]` - Recent cases, optionally filtered\n"
        "`{p}history view <case id>` - View one case\n"
        "`{p}history remove @user <case id>` - Remove a case (Manage Server)\n"
        "`{p}history removeall @user` - Remove all of a user's cases (Manage Server)\n"
        "`{p}notes @user` - View a user's notes\n"
        "`{p}notes add @user <note>` - Add a note\n"
        "`{p}notes remove @user <note id>` - Remove a note\n"
        "`{p}notes clear @user` - Clear all notes\n"
        "`{p}modstats [@mod]` - Moderator activity stats\n"
        "`{p}modhistory [@mod]` - Cases handled by a moderator\n"
        "`{p}whois [@user]` - Info about a user",

        # ---- page 2
        "# Moderation\n"
        "\n"
        "## Channels\n"
        "**Required:** Manage Channels\n"
        "`{p}lock [all]` - Lock this channel, or every channel (alias `l`)\n"
        "`{p}unlock [all]` - Unlock (alias `ul`)\n"
        "`{p}lockdown [all]` - Lock sending, reactions and threads\n"
        "`{p}hide` / `{p}unhide` - Hide or show this channel (alias `uh`)\n"
        "`{p}slowmode [on/off] [#channel] [seconds]` - Set or view slowmode\n"
        "`{p}topic <text>` - Set this channel's topic\n"
        "`{p}talk #channel @role` - Toggle whether a role can talk in a channel\n"
        "`{p}revokefiles on|off [#channel]` - Block or allow files and embeds\n"
        "\n"
        "## Threads and Voice\n"
        "`{p}thread rename <thread> <name>` - Rename a thread (Manage Threads)\n"
        "`{p}thread lock|unlock <thread>` - Lock or unlock a thread\n"
        "`{p}thread add|remove <thread> @member` - Add or remove a member\n"
        "`{p}moveall <voice channel>` - Move everyone in your voice channel (Move Members)\n"
        "`{p}drag <@member or ID> [more members] [voice channel or ID]` - Move members into a voice channel, or into your own call if you leave the channel out\n"
        "\n"
        "## Roles\n"
        "**Required:** Manage Roles\n"
        "`{p}role @role [@member]` - Toggle a role on a member (you by default)\n"
        "`{p}role bots @role [remove]` - Give or remove a role for every bot\n"
        "`{p}role humans @role` - Give a role to every human\n"
        "`{p}role create <name>` / `{p}role delete @role` - Create or delete a role\n"
        "`{p}role color <color> @role` - Change a role's color\n"
        "`{p}role color gradient <color> <second color> @role` - Gradient color\n"
        "`{p}role restore @member` - Restore a member's saved roles (alias `{p}rrs`)\n"
        "`{p}temprole @member <duration> @role` - Give a temporary role\n"
        "`{p}temprole` - List active temporary roles\n"
        "`{p}blist @member @role` - Block or unblock a member from a role\n"
        "`{p}inrole @role` - List members with a role\n"
        "`{p}roles` - List every role\n"
        "\n"
        "## Server Lists\n"
        "`{p}rename @member <nickname>` - Change a nickname (Manage Nicknames)\n"
        "`{p}bots` - List the bots in the server (Manage Server)\n"
        "`{p}membercount` - Show the member count (alias `mc`)",

        # ---- page 3
        "# Moderation\n"
        "\n"
        "## Messages\n"
        "**Required:** Manage Messages\n"
        "`{p}purge [amount]` - Delete recent messages (alias `c`)\n"
        "`{p}purge bots|humans|images|files|emotes|links|invites [amount]` - Delete one kind\n"
        "`{p}purge clear <amount> [@user]` - Delete messages, or just one user's\n"
        "`{p}pi [amount]` - Delete messages with images\n"
        "`{p}bc [amount]` - Delete bot and command messages\n"
        "`{p}snipe [n]` - Show a deleted message\n"
        "`{p}editsnipe [n]` - Show an edited message\n"
        "`{p}reactionsnipe [n]` - Show a removed reaction\n"
        "`{p}clearsnipe` - Clear snipe data here (alias `cs`)\n"
        "\n"
        "## Emojis, Stickers and Reminders\n"
        "`{p}emoji addmany <emojis>` - Add up to 25 custom emojis (Manage Expressions)\n"
        "`{p}sticker add <name>` - Add a sticker from an attached or replied image\n"
        "`{p}remind <duration> <text>` - Set a reminder (`list`, `remove <id>`)\n"
        "\n"
        "## Command Control\n"
        "**Required:** Manage Server\n"
        "`{p}disablecommand #channel|@member <command>` - Disable a command (alias `dcmd`)\n"
        "`{p}disablecommand all <command>` - Disable it server-wide\n"
        "`{p}disablecommand list` - View disabled commands\n"
        "`{p}enablecommand #channel|@member <command>` - Re-enable (alias `ecmd`)\n"
        "`{p}enablecommand all <command>` - Remove every block on a command\n"
        "`{p}ignore add|remove @member|#channel` - Make the bot ignore someone or a channel\n"
        "`{p}ignore list` - View ignored members and channels\n"
        "`{p}restrictcommand <command> @role` - Limit a command to a role (alias `rc`)\n"
        "`{p}restrictcommand add|remove <command> @role` - Add or remove a role\n"
        "`{p}restrictcommand list` / `reset` - View or clear restrictions\n"
        "\n"
        "## Auto Moderation\n"
        "**Required:** Manage Server\n"
        "`{p}filter` - View the filter settings\n"
        "`{p}filter spam|links|caps|invites <#channel/all> <on/off>` - Turn a filter on or off\n"
        "Optional: `--threshold N` and `--interval N`\n"
        "`{p}filter <type> exempt @role` - Exempt a role\n"
        "`{p}filter links whitelist #channel|all <domain>` - Allow a domain\n"
        "`{p}filter snipe <type>` - Keep a type of content out of snipe\n"
        "\n"
        "## Autorole\n"
        "**Required:** Manage Roles\n"
        "`{p}autorole add|remove @role` - Roles given to new members\n"
        "`{p}autorole list` / `reset` - View or clear them\n"
        "\n"
        "## Anti-Raid\n"
        "**Required:** Manage Server\n"
        "`{p}antiraid newaccounts on|off [days]` - Block very new accounts\n"
        "`{p}antiraid avatar on|off` - Block accounts without an avatar\n"
        "`{p}antiraid massjoin on|off [count] [seconds]` - Mass join protection\n"
        "`{p}antiraid whitelist @user` - Whitelist a user (`whitelist view`)\n"
        "`{p}antiraid config` - View the settings (alias `raidstate`)\n"
        "\n"
        "## Fake Permissions\n"
        "`{p}fakepermissions list [target]` - View them (alias `fp`, Manage Server)\n"
        "`{p}fakepermissions grant <role/member> <permissions>` - Grant (owner or admin)\n"
        "`{p}fakepermissions revoke <target> <permission>` - Revoke (owner or admin)\n"
        "`{p}fakepermissions reset` - Reset all (owner or admin)\n"
        "\n"
        "## Owner\n"
        "`{p}nuke` - Delete and recreate this channel to wipe its messages (owner, Anti-Nuke admins, Administrators)",

    ]

    def __init__(self, bot):
        self.bot = bot
        self.snipe_del = defaultdict(lambda: deque(maxlen=20))
        self.snipe_edit = defaultdict(lambda: deque(maxlen=20))
        self.snipe_react = defaultdict(lambda: deque(maxlen=20))
        self.join_times = defaultdict(list)
        self.spam_log = {}
        self.unban_tasks = {}

    # ---- lifecycle -------------------------------------------------------
    async def cog_load(self):
        self.bot.add_check(self.command_gate)
        self.expiry_loop.start()
        self.reminder_loop.start()

    async def cog_unload(self):
        self.bot.remove_check(self.command_gate)
        self.expiry_loop.cancel()
        self.reminder_loop.cancel()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, Blocked):
            if str(error):
                await send(ctx, str(error))
            return
        if isinstance(error, commands.MissingPermissions):
            msg = "You don't have permission to use that."
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                                commands.TooManyArguments, commands.BadUnionArgument)):
            msg = f"Usage: `{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`"
        elif isinstance(error, discord.Forbidden):
            msg = "I'm missing permissions or my role is too low for that."
        elif isinstance(error, commands.NoPrivateMessage):
            msg = "Server only."
        elif isinstance(error, commands.CheckFailure):
            msg = "You can't use that here."
        else:
            print(f"Error in {ctx.command}:", file=sys.stderr)
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
            msg = f"Something went wrong: `{error}`"
        await send(ctx, msg)

    # ---- internal helpers ------------------------------------------------
    async def guard(self, ctx, user) -> bool:
        if user.id == ctx.author.id:
            await send(ctx, "You can't do that to yourself.")
            return False
        if user.id == self.bot.user.id:
            await send(ctx, "Nice try.")
            return False
        m = ctx.guild.get_member(user.id)
        if m:
            if m == ctx.guild.owner or m.top_role >= ctx.guild.me.top_role:
                await send(ctx, "My top role isn't high enough to act on them.")
                return False
            if ctx.author != ctx.guild.owner and not is_bot_owner(ctx) and m.top_role >= ctx.author.top_role:
                await send(ctx, "You can't act on someone with an equal or higher role.")
                return False
        return True

    async def role_ok(self, ctx, role: discord.Role) -> bool:
        if role.managed or role >= ctx.guild.me.top_role:
            await send(ctx, "I can't manage that role (managed, or higher than my top role).")
            return False
        if ctx.author != ctx.guild.owner and not is_bot_owner(ctx) and role >= ctx.author.top_role:
            await send(ctx, "You can't manage a role equal to or higher than your top role.")
            return False
        return True

    def log_case(self, ctx, action, target, reason=None, duration=None):
        gid = str(ctx.guild.id)
        cid = db["counters"].get(f"case:{gid}", 0) + 1
        db["counters"][f"case:{gid}"] = cid
        db["cases"].setdefault(gid, []).append({
            "id": cid, "action": action, "user": target.id, "mod": ctx.author.id,
            "reason": reason or "No reason provided", "duration": duration, "at": int(time.time())})
        save()
        return cid

    async def jail_log(self, guild, text):
        cid = db["config"].get(str(guild.id), {}).get("jail_log")
        ch = guild.get_channel(cid) if cid else None
        if ch:
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def alert(self, guild, text):
        cid = db["config"].get(str(guild.id), {}).get("jail_log")
        ch = guild.get_channel(cid) if cid else guild.system_channel
        if ch:
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def get_jail_role(self, guild):
        role = discord.utils.get(guild.roles, name="Jail")
        if role:
            return role
        role = await guild.create_role(name="Jail", reason="Jail role setup")
        for ch in guild.channels:
            try:
                await ch.set_permissions(role, view_channel=False, send_messages=False)
            except discord.HTTPException:
                pass
        try:
            await guild.create_text_channel("jail", overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True)})
        except discord.HTTPException:
            pass
        return role

    async def release(self, guild, member, reason):
        role = discord.utils.get(guild.roles, name="Jail")
        saved = db["jails"].get(str(guild.id), {}).pop(str(member.id), [])
        save()
        if role and role in member.roles:
            await member.remove_roles(role, reason=reason)
        restore = [r for r in (guild.get_role(i) for i in saved) if r]
        if restore:
            await member.add_roles(*restore, reason=reason)

    async def ensure_mute_role(self, guild, name, force=False):
        role = discord.utils.get(guild.roles, name=name)
        created = role is None
        if created:
            role = await guild.create_role(name=name, reason="Mute role setup")
        if created or force:
            for ch in guild.channels:
                try:
                    await ch.set_permissions(role, **MUTE_ROLES[name])
                except discord.HTTPException:
                    pass
        return role

    async def set_lock(self, ctx, locked: bool, all_: bool, deep=False):
        everyone = ctx.guild.default_role
        targets = ctx.guild.text_channels if all_ else [ctx.channel]
        for ch in targets:
            ow = ch.overwrites_for(everyone)
            val = False if locked else None
            ow.send_messages = val
            if deep:
                ow.add_reactions = val
                ow.create_public_threads = val
                ow.send_messages_in_threads = val
            await ch.set_permissions(everyone, overwrite=ow)
        return len(targets)

    async def do_purge(self, ctx, check, amount):
        amount = max(1, min(amount, 1000))
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        # check=None would be called as a function and crash, so only pass a check when there is one
        deleted = await ctx.channel.purge(limit=amount, **({"check": check} if check else {}))
        await send(ctx, f"🧹 Deleted {len(deleted)} message(s).", delete_after=5)

    # ======================================================================
    # BACKGROUND LOOPS
    # ======================================================================
    @tasks.loop(seconds=15)
    async def expiry_loop(self):
        now = time.time()
        due = [t for t in db["timers"] if t["at"] <= now]
        if not due:
            return
        db["timers"] = [t for t in db["timers"] if t["at"] > now]
        save()
        for t in due:
            guild = self.bot.get_guild(t["guild"])
            if not guild:
                continue
            try:
                if t["kind"] == "tempban":
                    await guild.unban(discord.Object(t["user"]), reason="Tempban expired")
                elif t["kind"] == "jail":
                    m = guild.get_member(t["user"])
                    if m:
                        await self.release(guild, m, "Jail expired")
                elif t["kind"] in ("temprole", "unmute"):
                    m, r = guild.get_member(t["user"]), guild.get_role(t["role"])
                    if m and r:
                        await m.remove_roles(r, reason="Temporary role expired")
            except discord.HTTPException:
                pass

    @expiry_loop.before_loop
    async def _before_expiry(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=10)
    async def reminder_loop(self):
        now = time.time()
        due = [r for r in db["reminders"] if r["at"] <= now]
        if not due:
            return
        db["reminders"] = [r for r in db["reminders"] if r["at"] > now]
        save()
        for r in due:
            text = f"⏰ <@{r['user']}> reminder: {r['text']}"
            try:
                ch = self.bot.get_channel(r["channel"])
                if ch:
                    await ch.send(text, allowed_mentions=discord.AllowedMentions(users=True))
                else:
                    user = await self.bot.fetch_user(r["user"])
                    await user.send(text)
            except discord.HTTPException:
                pass

    @reminder_loop.before_loop
    async def _before_reminder(self):
        await self.bot.wait_until_ready()

    # ======================================================================
    # LISTENERS
    # ======================================================================
    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        """Hardbans are protected: re-apply if someone unbans manually."""
        if str(user.id) in db["hardbans"].get(str(guild.id), {}):
            try:
                await guild.ban(user, reason="Hardban: protected ban re-applied")
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        blocked = db["blist"].get(str(after.guild.id), {}).get(str(after.id), [])
        if not blocked:
            return
        for r in set(after.roles) - set(before.roles):
            if r.id in blocked:
                try:
                    await after.remove_roles(r, reason="User is on the role blocklist")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        roles = [r.id for r in member.roles if r != member.guild.default_role and not r.managed]
        if roles:
            db["saved_roles"].setdefault(str(member.guild.id), {})[str(member.id)] = roles
            save()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        g = member.guild
        conf = db["antiraid"].get(str(g.id))
        if conf and not member.bot and member.id not in conf["whitelist"]:
            reason = None
            age = (discord.utils.utcnow() - member.created_at).total_seconds() / 86400
            if conf["newaccounts"]["on"] and age < conf["newaccounts"]["days"]:
                reason = f"Anti-raid: account younger than {conf['newaccounts']['days']} day(s)"
            elif conf["avatar"] and member.avatar is None:
                reason = "Anti-raid: no avatar"
            elif conf["massjoin"]["on"]:
                mj, now = conf["massjoin"], time.time()
                jt = [(t, u) for t, u in self.join_times[g.id] if now - t <= mj["seconds"]] + [(now, member.id)]
                self.join_times[g.id] = jt
                if now < conf.get("raid_until", 0):
                    reason = "Anti-raid: raid state active"
                elif len(jt) >= mj["count"]:
                    conf["raid_until"] = now + 600
                    save()
                    self.join_times[g.id] = []
                    await self.alert(g, f"🚨 Possible raid: {len(jt)} joins within {mj['seconds']}s. "
                                        f"Raid state on for 10 minutes; new joins will be kicked.")
                    for _, uid in jt:
                        if uid not in conf["whitelist"]:
                            await self.kick_quiet(g, uid, "Anti-raid: mass join")
                    return
            if reason:
                await self.kick_quiet(g, member.id, reason)
                await self.alert(g, f"🛡️ Kicked **{member}** (`{member.id}`) — {reason}")
                return
        ids = db["autoroles"].get(str(g.id), [])
        roles = [r for r in (g.get_role(i) for i in ids) if r and not r.managed and r < g.me.top_role]
        if roles:
            try:
                await member.add_roles(*roles, reason="Autorole")
            except discord.HTTPException:
                pass

    async def kick_quiet(self, guild, user_id, reason):
        try:
            await guild.kick(discord.Object(user_id), reason=reason)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        await self.run_filters(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.guild and not message.author.bot:
            self.snipe_del[message.channel.id].appendleft({
                "author": message.author, "content": message.content,
                "files": [a.url for a in message.attachments], "at": discord.utils.utcnow()})

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if after.guild and not after.author.bot and before.content != after.content:
            self.snipe_edit[after.channel.id].appendleft({
                "author": after.author, "before": before.content, "after": after.content,
                "url": after.jump_url, "at": discord.utils.utcnow()})

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.guild_id:
            self.snipe_react[payload.channel_id].appendleft({
                "user": payload.user_id, "emoji": str(payload.emoji), "guild": payload.guild_id,
                "channel": payload.channel_id, "message": payload.message_id,
                "at": discord.utils.utcnow()})

    # ---- bot-wide gate for disable / ignore / restrict --------------------
    async def command_gate(self, ctx):
        if ctx.guild is None or ctx.command is None:
            return True
        if is_bot_owner(ctx):
            return True
        gid = str(ctx.guild.id)
        name = (ctx.command.root_parent or ctx.command).name
        if name in MANAGE_CMDS:
            return True
        ign = db["ignores"].get(gid, {})
        if (ctx.channel.id in ign.get("channels", []) or ctx.author.id in ign.get("members", [])) \
                and not ctx.author.guild_permissions.manage_guild:
            return await self._block(ctx, "")
        dis = db["disabled"].get(gid, {})
        if (name in dis.get("all", []) or name in dis.get("channels", {}).get(str(ctx.channel.id), [])
                or name in dis.get("members", {}).get(str(ctx.author.id), [])):
            return await self._block(ctx, "That command is disabled here.")
        allowed = db["restrict"].get(gid, {}).get(name)
        if allowed and not ctx.author.guild_permissions.administrator \
                and not any(r.id in allowed for r in ctx.author.roles):
            return await self._block(ctx, "You don't have a role that's allowed to use that command.")
        return True

    async def _block(self, ctx, msg):
        if ctx.command.cog is self:
            raise Blocked(msg)
        if msg:
            await send(ctx, msg)
        return False

    # ======================================================================
    # MEMBER ACTIONS  (kick, ban, hardban, timeout: no aliases)
    # ======================================================================
    @commands.command()
    @has_perms(ban_members=True)
    async def ban(self, ctx, user: discord.User, *, reason: str = None):
        if not await self.guard(ctx, user):
            return
        async with notify(ctx, user, "ban", reason):
            await ctx.guild.ban(user, reason=why(ctx, reason))
        self.log_case(ctx, "ban", user, reason)
        await send(ctx, f"🔨 Banned **{user}** — {reason or 'No reason provided'}")

    @commands.command()
    @has_perms(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = None):
        if not await self.guard(ctx, member):
            return
        async with notify(ctx, member, "kick", reason):
            await member.kick(reason=why(ctx, reason))
        self.log_case(ctx, "kick", member, reason)
        await send(ctx, f"👢 Kicked **{member}** — {reason or 'No reason provided'}")

    @commands.command()
    @has_perms(ban_members=True)
    async def tempban(self, ctx, user: discord.User, duration: str, *, reason: str = None):
        secs = parse_duration(duration)
        if not secs:
            return await send(ctx, "Duration like `30m`, `12h`, `7d`.")
        if not await self.guard(ctx, user):
            return
        async with notify(ctx, user, "tempban", reason, duration):
            await ctx.guild.ban(user, reason=why(ctx, f"[tempban {duration}] {reason or ''}"))
        add_timer("tempban", ctx.guild.id, user.id, secs)
        self.log_case(ctx, "tempban", user, reason, duration)
        await send(ctx, f"⏳ Temp-banned **{user}** for {duration}.")

    @commands.command()
    @has_perms(ban_members=True)
    async def softban(self, ctx, user: discord.User, *, reason: str = None):
        if not await self.guard(ctx, user):
            return
        async with notify(ctx, user, "softban", reason):
            await ctx.guild.ban(user, reason=why(ctx, f"[softban] {reason or ''}"), delete_message_seconds=86400)
            await ctx.guild.unban(user, reason="Softban")
        self.log_case(ctx, "softban", user, reason)
        await send(ctx, f"🧹 Softbanned **{user}** (messages cleared, ban lifted).")

    @commands.group(name="hardban", invoke_without_command=True)
    @has_perms(ban_members=True)
    async def hardban(self, ctx, user: discord.User, *, reason: str = None):
        if not await self.guard(ctx, user):
            return
        db["hardbans"].setdefault(str(ctx.guild.id), {})[str(user.id)] = {
            "mod": ctx.author.id, "reason": reason or "No reason provided", "at": int(time.time())}
        save()
        async with notify(ctx, user, "hardban", reason):
            await ctx.guild.ban(user, reason=why(ctx, f"[hardban] {reason or ''}"))
        self.log_case(ctx, "hardban", user, reason)
        await send(ctx, f"🔒 Hardbanned **{user}**. Manual unbans will be reverted.")

    @hardban.command(name="list")
    @has_perms(ban_members=True)
    async def hardban_list(self, ctx):
        hb = db["hardbans"].get(str(ctx.guild.id), {})
        if not hb:
            return await send(ctx, "No protected bans.")
        lines = [f"`{uid}` — {d['reason']}" for uid, d in hb.items()]
        await send(ctx, "**Protected bans**\n" + "\n".join(lines)[:1900])

    @hardban.command(name="clear")
    @has_perms(manage_guild=True)
    async def hardban_clear(self, ctx):
        n = len(db["hardbans"].pop(str(ctx.guild.id), {}))
        save()
        await send(ctx, f"Cleared {n} hardban record(s). Existing Discord bans stay until unbanned.")

    @commands.command(name="unforeverban", aliases=["unfb", "unhardban"])
    @has_perms(ban_members=True)
    async def unforeverban(self, ctx, user: discord.User):
        db["hardbans"].get(str(ctx.guild.id), {}).pop(str(user.id), None)
        save()
        try:
            await ctx.guild.unban(user, reason=why(ctx, "unforeverban"))
        except discord.NotFound:
            pass
        self.log_case(ctx, "unhardban", user)
        await send(ctx, f"✅ Removed hardban for **{user}**.")

    @commands.command(aliases=["ub"])
    @has_perms(ban_members=True)
    async def unban(self, ctx, user: discord.User):
        if str(user.id) in db["hardbans"].get(str(ctx.guild.id), {}):
            return await send(ctx, f"That's a protected ban. Use `{ctx.clean_prefix}unforeverban` first.")
        try:
            await ctx.guild.unban(user, reason=why(ctx, "unban"))
        except discord.NotFound:
            return await send(ctx, "That user isn't banned.")
        self.log_case(ctx, "unban", user)
        await send(ctx, f"✅ Unbanned **{user}**.")

    @commands.command()
    @has_perms(administrator=True)
    async def unbanall(self, ctx, arg: Optional[str] = None):
        gid = ctx.guild.id
        if arg and arg.lower() == "cancel":
            task = self.unban_tasks.pop(gid, None)
            if task and not task.done():
                task.cancel()
                return await send(ctx, "🛑 Stopped unbanall.")
            return await send(ctx, "No unbanall running.")
        if gid in self.unban_tasks and not self.unban_tasks[gid].done():
            return await send(ctx, f"Already running. Use `{ctx.clean_prefix}unbanall cancel` to stop.")

        async def run():
            protected = set(db["hardbans"].get(str(gid), {}))
            count = 0
            async for entry in ctx.guild.bans(limit=None):
                if str(entry.user.id) in protected:
                    continue
                await ctx.guild.unban(entry.user, reason="unbanall")
                count += 1
                await asyncio.sleep(0.6)
            await send(ctx, f"✅ Unbanned {count} user(s). Protected bans were skipped.")

        self.unban_tasks[gid] = asyncio.create_task(run())
        await send(ctx, "Unbanning everyone except protected bans…")

    # ---- warnings ---------------------------------------------------------
    @commands.command()
    @has_perms(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = None):
        if not await self.guard(ctx, member):
            return
        w = db["warns"].setdefault(str(ctx.guild.id), {}).setdefault(str(member.id), [])
        w.append({"mod": ctx.author.id, "reason": reason or "No reason provided", "at": int(time.time())})
        save()
        self.log_case(ctx, "warn", member, reason)
        await dm_punishment(ctx.guild, ctx.author, member, "warn", reason)
        await send(ctx, f"⚠️ Warned **{member}** (#{len(w)}) — {reason or 'No reason provided'}")

    @commands.command(name="warnings")
    @has_perms(moderate_members=True)
    async def warnings_cmd(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        w = db["warns"].get(str(ctx.guild.id), {}).get(str(member.id), [])
        if not w:
            return await send(ctx, f"**{member}** has no warnings.")
        lines = [f"`{i}.` <t:{x['at']}:d> by <@{x['mod']}> — {x['reason']}" for i, x in enumerate(w, 1)]
        await send(ctx, embed=discord.Embed(title=f"Warnings for {member}", description="\n".join(lines)[:4000]))

    @commands.command()
    @has_perms(moderate_members=True)
    async def clearwarnings(self, ctx, member: discord.Member):
        n = len(db["warns"].get(str(ctx.guild.id), {}).pop(str(member.id), []))
        save()
        await send(ctx, f"🧽 Cleared {n} warning(s) for **{member}**.")

    # ---- timeout (no aliases) ---------------------------------------------
    @commands.command()
    @has_perms(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, duration: str, *, reason: str = None):
        secs = parse_duration(duration)
        if not secs or secs > 28 * 86400:
            return await send(ctx, "Duration like `10m`, `2h`, `3d` (max 28d).")
        if not await self.guard(ctx, member):
            return
        async with notify(ctx, member, "timeout", reason, duration):
            await member.timeout(timedelta(seconds=secs), reason=why(ctx, reason))
        self.log_case(ctx, "timeout", member, reason, duration)
        await send(ctx, f"🔇 Timed out **{member}** for {duration}.")

    @commands.command(aliases=["uto"])
    @has_perms(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = None):
        await member.timeout(None, reason=why(ctx, reason))
        self.log_case(ctx, "untimeout", member, reason)
        await send(ctx, f"🔊 Removed timeout from **{member}**.")

    # ---- jail ---------------------------------------------------------------
    @commands.command()
    @has_perms(manage_roles=True)
    async def jail(self, ctx, member: discord.Member, *, rest: str = ""):
        secs, label, reason = split_duration(rest)
        if not await self.guard(ctx, member):
            return
        role = await self.get_jail_role(ctx.guild)
        me_top = ctx.guild.me.top_role
        strip = [r for r in member.roles
                 if r != ctx.guild.default_role and not r.managed and r < me_top and r != role]
        db["jails"].setdefault(str(ctx.guild.id), {})[str(member.id)] = [r.id for r in strip]
        save()
        async with notify(ctx, member, "jail", reason, label):
            if strip:
                await member.remove_roles(*strip, reason=why(ctx, reason))
            await member.add_roles(role, reason=why(ctx, reason))
        if secs:
            add_timer("jail", ctx.guild.id, member.id, secs)
        self.log_case(ctx, "jail", member, reason, label)
        await self.jail_log(ctx.guild, f"⛓️ {ctx.author} jailed {member} — {reason or 'No reason provided'}")
        await send(ctx, f"⛓️ Jailed **{member}**" + (f" for {label}." if label else "."))

    @commands.command()
    @has_perms(manage_roles=True)
    async def unjail(self, ctx, member: discord.Member, *, reason: str = None):
        await self.release(ctx.guild, member, why(ctx, reason))
        remove_timers("jail", ctx.guild.id, member.id)
        self.log_case(ctx, "unjail", member, reason)
        await self.jail_log(ctx.guild, f"🔓 {ctx.author} released {member}")
        await send(ctx, f"🔓 Released **{member}**.")

    @commands.command()
    @has_perms(manage_roles=True)
    async def jaillist(self, ctx):
        ids = db["jails"].get(str(ctx.guild.id), {})
        if not ids:
            return await send(ctx, "Nobody is jailed.")
        await send(ctx, "**Jailed members**\n" + "\n".join(f"<@{i}>" for i in ids))

    # ---- role-related member actions ---------------------------------------
    @commands.command()
    @has_perms(manage_roles=True)
    async def stripstaff(self, ctx, member: discord.Member):
        if not await self.guard(ctx, member):
            return
        roles = [r for r in member.roles
                 if r != ctx.guild.default_role and not r.managed and r < ctx.guild.me.top_role
                 and any(getattr(r.permissions, p) for p in STAFF_PERMS)]
        if not roles:
            return await send(ctx, "No removable staff roles found.")
        await member.remove_roles(*roles, reason=why(ctx, "stripstaff"))
        await send(ctx, f"🪖 Removed {len(roles)} staff role(s) from **{member}**.")

    @commands.command()
    @has_perms(manage_roles=True)
    async def blist(self, ctx, member: discord.Member, role: discord.Role):
        """Toggle: block (or unblock) a member from receiving a role."""
        lst = db["blist"].setdefault(str(ctx.guild.id), {}).setdefault(str(member.id), [])
        if role.id in lst:
            lst.remove(role.id)
            msg = f"✅ **{member}** can receive **{role.name}** again."
        else:
            lst.append(role.id)
            if role in member.roles:
                await member.remove_roles(role)
            msg = f"🚫 **{member}** is now blocked from **{role.name}**."
        save()
        await send(ctx, msg)

    @commands.command()
    @has_perms(manage_roles=True)
    async def temprole(self, ctx, member: Optional[discord.Member] = None,
                       duration: Optional[str] = None, role: Optional[discord.Role] = None):
        """With member+duration+role: give a temp role. Otherwise list active temp roles."""
        if member and duration and role:
            secs = parse_duration(duration)
            if not secs:
                return await send(ctx, "Duration like `1h`, `3d`.")
            if not await self.role_ok(ctx, role):
                return
            await member.add_roles(role, reason=why(ctx, f"temprole {duration}"))
            add_timer("temprole", ctx.guild.id, member.id, secs, role.id)
            return await send(ctx, f"⏱️ Gave **{member}** **{role.name}** for {duration}.")
        items = [t for t in db["timers"] if t["kind"] == "temprole" and t["guild"] == ctx.guild.id
                 and (not member or t["user"] == member.id)]
        if not items:
            return await send(ctx, "No active temporary roles.")
        lines = [f"<@{t['user']}> — <@&{t['role']}> expires <t:{int(t['at'])}:R>" for t in items]
        await send(ctx, "\n".join(lines)[:1900])

    # ======================================================================
    # CHANNELS, THREADS, VOICE
    # ======================================================================
    @commands.command(aliases=["l"])
    @has_perms(manage_channels=True)
    async def lock(self, ctx, scope: Optional[str] = None):
        n = await self.set_lock(ctx, True, is_all(scope))
        await send(ctx, f"🔒 Locked {n} channel(s).")

    @commands.command(aliases=["ul"])
    @has_perms(manage_channels=True)
    async def unlock(self, ctx, scope: Optional[str] = None):
        n = await self.set_lock(ctx, False, is_all(scope), deep=True)
        await send(ctx, f"🔓 Unlocked {n} channel(s).")

    @commands.command()
    @has_perms(manage_channels=True)
    async def lockdown(self, ctx, scope: Optional[str] = None):
        n = await self.set_lock(ctx, True, is_all(scope), deep=True)
        await send(ctx, f"🚨 Lockdown: {n} channel(s) locked (sending, reactions and threads). "
                        f"Use `{ctx.clean_prefix}unlock` to lift.")

    @commands.command()
    @has_perms(manage_channels=True)
    async def hide(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=False)
        await send(ctx, "🙈 Channel hidden from @everyone.")

    @commands.command(aliases=["uh"])
    @has_perms(manage_channels=True)
    async def unhide(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, view_channel=None)
        await send(ctx, "👀 Channel visible again.")

    @commands.command()
    @has_perms(manage_channels=True)
    async def slowmode(self, ctx, *, args: str = ""):
        channel, mode, seconds = ctx.channel, None, None
        for tok in args.split():
            if tok.lower() in ("on", "off"):
                mode = tok.lower()
            elif tok.isdigit():
                seconds = int(tok)
            else:
                try:
                    channel = await commands.TextChannelConverter().convert(ctx, tok)
                except commands.BadArgument:
                    return await send(ctx, f"Usage: `{ctx.clean_prefix}slowmode [on/off] [#channel] [seconds]`")
        if mode is None and seconds is None:
            return await send(ctx, f"Slowmode in {channel.mention}: **{channel.slowmode_delay}s**.")
        seconds = 0 if mode == "off" else (seconds if seconds is not None else 5)
        seconds = min(seconds, 21600)
        await channel.edit(slowmode_delay=seconds)
        await send(ctx, f"🐌 Slowmode in {channel.mention} set to **{seconds}s**.")

    @commands.command(name="topic")
    @has_perms(manage_channels=True)
    async def topic_cmd(self, ctx, *, text: str):
        await ctx.channel.edit(topic=text[:1024])
        await send(ctx, "📝 Topic updated.")

    @commands.command()
    @has_perms(manage_channels=True)
    async def talk(self, ctx, channel: discord.TextChannel, role: discord.Role):
        ow = channel.overwrites_for(role)
        ow.send_messages = True if ow.send_messages is False else False
        await channel.set_permissions(role, overwrite=ow)
        state = "can now talk" if ow.send_messages else "can no longer talk"
        await send(ctx, f"💬 **{role.name}** {state} in {channel.mention}.")

    @commands.group(invoke_without_command=True)
    @has_perms(manage_threads=True)
    async def thread(self, ctx):
        p = ctx.clean_prefix
        await send(ctx, f"Usage: `{p}thread rename|lock|unlock <thread> [value]` or "
                        f"`{p}thread add|remove <thread> @user`")

    @thread.command(name="rename")
    @has_perms(manage_threads=True)
    async def thread_rename(self, ctx, th: discord.Thread, *, value: str):
        await th.edit(name=value[:100])
        await send(ctx, f"✏️ Renamed to **{value[:100]}**.")

    @thread.command(name="lock")
    @has_perms(manage_threads=True)
    async def thread_lock(self, ctx, th: discord.Thread, *, value: str = None):
        await th.edit(locked=True)
        await send(ctx, f"🔒 Locked {th.mention}.")

    @thread.command(name="unlock")
    @has_perms(manage_threads=True)
    async def thread_unlock(self, ctx, th: discord.Thread, *, value: str = None):
        await th.edit(locked=False)
        await send(ctx, f"🔓 Unlocked {th.mention}.")

    @thread.command(name="add")
    @has_perms(manage_threads=True)
    async def thread_add(self, ctx, th: discord.Thread, member: discord.Member):
        await th.add_user(member)
        await send(ctx, f"➕ Added **{member}** to {th.mention}.")

    @thread.command(name="remove")
    @has_perms(manage_threads=True)
    async def thread_remove(self, ctx, th: discord.Thread, member: discord.Member):
        await th.remove_user(member)
        await send(ctx, f"➖ Removed **{member}** from {th.mention}.")

    @commands.command()
    @has_perms(move_members=True)
    async def moveall(self, ctx, channel: discord.VoiceChannel):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await send(ctx, "Join a voice channel first.")
        members = list(ctx.author.voice.channel.members)
        for m in members:
            await m.move_to(channel)
        await send(ctx, f"🚚 Moved {len(members)} member(s) to **{channel.name}**.")

    @commands.command()
    @has_perms(move_members=True)
    async def drag(self, ctx, *targets: str):
        # members and the channel can each be a mention, a user/channel ID or a name;
        # with no channel given they're dragged into the call you're in
        members, channel, unknown = [], None, []
        for token in targets:
            found = None
            for conv in (commands.MemberConverter, commands.VoiceChannelConverter, commands.StageChannelConverter):
                try:
                    found = await conv().convert(ctx, token)
                    break
                except commands.BadArgument:
                    continue
            if isinstance(found, discord.Member):
                if found not in members:
                    members.append(found)
            elif found is not None:
                channel = found
            else:
                unknown.append(token)

        if unknown:
            shown = ", ".join(f"`{t[:40]}`" for t in unknown[:5])
            return await send(ctx, f"I couldn't find {shown} as a member or voice channel in this server.")
        if not members:
            return await send(ctx, f"Usage: `{ctx.clean_prefix}drag <@member or ID> [more members] [voice channel or ID]`")
        if channel is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                return await send(ctx, "Join a voice channel first, or give me one: `drag <member> <voice channel or ID>`.")
            channel = ctx.author.voice.channel

        moved = 0
        for m in members:
            if m.voice and m.voice.channel and m.voice.channel != channel:
                try:
                    await m.move_to(channel, reason=why(ctx, "drag"))
                    moved += 1
                except discord.HTTPException:
                    pass
        await send(ctx, f"🚚 Moved {moved} member(s) to **{channel.name}**.")

    # ======================================================================
    # ROLES AND SERVER LISTS   (the `role` group has no "r" alias)
    # ======================================================================
    @commands.group(name="role", invoke_without_command=True)
    @has_perms(manage_roles=True)
    async def role_cmd(self, ctx, role: discord.Role, member: Optional[discord.Member] = None):
        member = member or ctx.author
        if not await self.role_ok(ctx, role):
            return
        if role in member.roles:
            await member.remove_roles(role, reason=why(ctx, "role toggle"))
            await send(ctx, f"➖ Removed **{role.name}** from **{member}**.")
        else:
            await member.add_roles(role, reason=why(ctx, "role toggle"))
            await send(ctx, f"➕ Gave **{role.name}** to **{member}**.")

    @role_cmd.command(name="bots")
    @has_perms(manage_roles=True)
    async def role_bots(self, ctx, role: discord.Role, action: Optional[str] = None):
        if not await self.role_ok(ctx, role):
            return
        remove = bool(action) and action.lower() == "remove"
        await send(ctx, "Working on it…")
        n = 0
        for m in ctx.guild.members:
            if m.bot and ((role in m.roles) == remove):
                await (m.remove_roles if remove else m.add_roles)(role)
                n += 1
                await asyncio.sleep(0.4)
        await send(ctx, f"Done — {'removed from' if remove else 'added to'} {n} bot(s).")

    @role_cmd.command(name="humans")
    @has_perms(manage_roles=True)
    async def role_humans(self, ctx, *, role: discord.Role):
        if not await self.role_ok(ctx, role):
            return
        await send(ctx, "Working on it… (this can take a while on big servers)")
        n = 0
        for m in ctx.guild.members:
            if not m.bot and role not in m.roles:
                await m.add_roles(role)
                n += 1
                await asyncio.sleep(0.4)
        await send(ctx, f"Done — gave **{role.name}** to {n} human(s).")

    @role_cmd.command(name="create")
    @has_perms(manage_roles=True)
    async def role_create(self, ctx, *, name: str):
        r = await ctx.guild.create_role(name=name[:100], reason=why(ctx, "role create"))
        await send(ctx, f"✅ Created {r.mention}.")

    @role_cmd.command(name="delete")
    @has_perms(manage_roles=True)
    async def role_delete(self, ctx, *, role: discord.Role):
        if not await self.role_ok(ctx, role):
            return
        await send(ctx, f"Type `yes` within 20s to delete **{role.name}**.")
        try:
            await self.bot.wait_for(
                "message", timeout=20,
                check=lambda m: m.author == ctx.author and m.channel == ctx.channel
                and m.content.lower() in ("yes", "y"))
        except asyncio.TimeoutError:
            return await send(ctx, "Cancelled.")
        await role.delete(reason=why(ctx, "role delete"))
        await send(ctx, "🗑️ Role deleted.")

    @role_cmd.group(name="color", aliases=["colour"], invoke_without_command=True)
    @has_perms(manage_roles=True)
    async def role_color(self, ctx, color: str, *, role: discord.Role):
        c = parse_color(color)
        if c is None:
            return await send(ctx, "Use a hex like `#ff8800` or a name: " + ", ".join(COLORS))
        if not await self.role_ok(ctx, role):
            return
        await role.edit(colour=discord.Colour(c))
        await send(ctx, f"🎨 Set **{role.name}** to `#{c:06x}`.")

    @role_color.command(name="gradient")
    @has_perms(manage_roles=True)
    async def role_gradient(self, ctx, color: str, second_color: str, *, role: discord.Role):
        c1, c2 = parse_color(color), parse_color(second_color)
        if c1 is None or c2 is None:
            return await send(ctx, "Use hex colors like `#ff8800` or names: " + ", ".join(COLORS))
        if not await self.role_ok(ctx, role):
            return
        route = discord.http.Route("PATCH", "/guilds/{guild_id}/roles/{role_id}",
                                   guild_id=ctx.guild.id, role_id=role.id)
        try:
            await self.bot.http.request(
                route, json={"colors": {"primary_color": c1, "secondary_color": c2}},
                reason=why(ctx, "role gradient"))
        except discord.HTTPException as e:
            return await send(ctx, f"Discord refused the gradient (the server may need to unlock it): `{e.text}`")
        await send(ctx, f"🌈 Set **{role.name}** gradient `#{c1:06x}` → `#{c2:06x}`.")

    @role_cmd.command(name="restore")
    @has_perms(manage_roles=True)
    async def role_restore(self, ctx, member: discord.Member):
        ids = db["saved_roles"].get(str(ctx.guild.id), {}).get(str(member.id))
        if not ids:
            return await send(ctx, "No saved roles for that member.")
        roles = [r for r in (ctx.guild.get_role(i) for i in ids)
                 if r and r not in member.roles and not r.managed and r < ctx.guild.me.top_role
                 and (ctx.author == ctx.guild.owner or is_bot_owner(ctx) or r < ctx.author.top_role)]
        if not roles:
            return await send(ctx, "Nothing I can restore (roles gone, or above my/your top role).")
        await member.add_roles(*roles, reason=why(ctx, "role restore"))
        await send(ctx, f"♻️ Restored {len(roles)} role(s) to **{member}**.")

    @commands.command(name="rrs")
    @has_perms(manage_roles=True)
    async def rrs(self, ctx, member: discord.Member):
        await ctx.invoke(self.role_restore, member=member)

    @commands.command(name="roles")
    @has_perms(manage_roles=True)
    async def roles_cmd(self, ctx):
        rs = [r for r in reversed(ctx.guild.roles) if r != ctx.guild.default_role]
        lines = [f"{r.mention} — {len(r.members)}" for r in rs]
        await send(ctx, embed=discord.Embed(title=f"Roles ({len(rs)})", description="\n".join(lines)[:4000]))

    @commands.command(name="bots")
    @has_perms(manage_guild=True)
    async def bots_cmd(self, ctx):
        bs = [m for m in ctx.guild.members if m.bot]
        desc = "\n".join(f"{m.mention} (`{m.id}`)" for m in bs) or "None"
        await send(ctx, embed=discord.Embed(title=f"Bots ({len(bs)})", description=desc[:4000]))

    @commands.command(aliases=["mc"])
    async def membercount(self, ctx):
        g = ctx.guild
        bots = sum(m.bot for m in g.members)
        await send(ctx, f"👥 **{g.member_count}** members ({g.member_count - bots} humans, {bots} bots)")

    @commands.command()
    @has_perms(manage_nicknames=True)
    async def rename(self, ctx, member: discord.Member, *, nickname: str):
        if not await self.guard(ctx, member):
            return
        await member.edit(nick=nickname[:32], reason=why(ctx, "rename"))
        await send(ctx, f"✏️ Renamed **{member.name}** to **{nickname[:32]}**.")

    @commands.command()
    @has_perms(manage_roles=True)
    async def inrole(self, ctx, *, role: discord.Role):
        ms = role.members
        lines = [f"{m.mention} (`{m.id}`)" for m in ms]
        await send(ctx, embed=discord.Embed(title=f"{role.name} — {len(ms)} member(s)",
                                           description="\n".join(lines)[:4000] or "Nobody."))

    # ======================================================================
    # MESSAGES AND EVIDENCE
    # ======================================================================
    @commands.group(aliases=["c"], invoke_without_command=True)
    @has_perms(manage_messages=True)
    async def purge(self, ctx, kind: str = "10", amount: Optional[int] = None):
        """purge [amount]  |  purge bots|humans|images|files|emotes|links|invites [amount]"""
        if kind.isdigit():
            return await self.do_purge(ctx, None, int(kind))
        check = FILTERS.get(kind.lower())
        if not check:
            return await send(ctx, f"Usage: `{ctx.clean_prefix}purge [amount]` or "
                                   f"`{ctx.clean_prefix}purge {'|'.join(FILTERS)} [amount]`")
        await self.do_purge(ctx, check, amount or 100)

    @purge.command(name="clear")
    @has_perms(manage_messages=True)
    async def purge_clear(self, ctx, amount: int, user: Optional[discord.User] = None):
        if not user:
            return await self.do_purge(ctx, None, amount)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        count = 0

        def check(m):
            nonlocal count
            if m.author.id == user.id and count < amount:
                count += 1
                return True
            return False

        deleted = await ctx.channel.purge(limit=1000, check=check)
        await send(ctx, f"🧹 Deleted {len(deleted)} message(s) from **{user}**.", delete_after=5)

    @commands.command(name="pi")
    @has_perms(manage_messages=True)
    async def purge_images(self, ctx, amount: int = 100):
        await self.do_purge(ctx, FILTERS["images"], amount)

    @commands.command()
    @has_perms(manage_messages=True)
    async def bc(self, ctx, amount: int = 50):
        """Remove bot messages and prefix-command messages."""
        prefixes = await self.bot.get_prefix(ctx.message)
        prefixes = tuple([prefixes] if isinstance(prefixes, str) else prefixes)
        await self.do_purge(ctx, lambda m: m.author.bot or m.content.startswith(prefixes), amount)

    def snipe_blocked(self, guild_id, text, files):
        conf = db["snipe_filters"].get(str(guild_id))
        if not conf:
            return False
        low = (text or "").lower()
        types = conf.get("types", [])
        if "invites" in types and INVITE_RE.search(text or ""):
            return True
        if "links" in types and URL_RE.search(text or ""):
            return True
        if "images" in types and any(f.lower().split("?")[0].endswith(IMG_EXT) for f in files):
            return True
        if "files" in types and files:
            return True
        if "music" in types and (MUSIC_RE.search(text or "")
                                 or any(f.lower().split("?")[0].endswith(AUDIO_EXT) for f in files)):
            return True
        return any(w in low for w in conf.get("words", []))

    @commands.command()
    @has_perms(manage_messages=True)
    async def snipe(self, ctx, n: int = 1):
        q = [e for e in self.snipe_del[ctx.channel.id]
             if not self.snipe_blocked(ctx.guild.id, e["content"], e["files"])]
        if n < 1 or len(q) < n:
            return await send(ctx, "Nothing to snipe.")
        e = q[n - 1]
        emb = discord.Embed(description=e["content"] or "*no text*", timestamp=e["at"])
        emb.set_author(name=str(e["author"]), icon_url=e["author"].display_avatar.url)
        if e["files"]:
            emb.add_field(name="Attachments", value="\n".join(e["files"])[:1000])
        await send(ctx, embed=emb)

    @commands.command()
    @has_perms(manage_messages=True)
    async def editsnipe(self, ctx, n: int = 1):
        q = [e for e in self.snipe_edit[ctx.channel.id]
             if not self.snipe_blocked(ctx.guild.id, e["before"] + " " + e["after"], [])]
        if n < 1 or len(q) < n:
            return await send(ctx, "Nothing to snipe.")
        e = q[n - 1]
        emb = discord.Embed(timestamp=e["at"])
        emb.set_author(name=str(e["author"]), icon_url=e["author"].display_avatar.url)
        emb.add_field(name="Before", value=e["before"][:1000] or "*empty*", inline=False)
        emb.add_field(name="After", value=e["after"][:1000] or "*empty*", inline=False)
        emb.add_field(name="Message", value=f"[Jump]({e['url']})")
        await send(ctx, embed=emb)

    @commands.command()
    @has_perms(manage_messages=True)
    async def reactionsnipe(self, ctx, n: int = 1):
        q = self.snipe_react[ctx.channel.id]
        if n < 1 or len(q) < n:
            return await send(ctx, "Nothing to snipe.")
        e = q[n - 1]
        link = f"https://discord.com/channels/{e['guild']}/{e['channel']}/{e['message']}"
        await send(ctx, embed=discord.Embed(
            description=f"<@{e['user']}> removed {e['emoji']} from [this message]({link})",
            timestamp=e["at"]))

    @commands.command(aliases=["cs"])
    @has_perms(manage_messages=True)
    async def clearsnipe(self, ctx):
        for d in (self.snipe_del, self.snipe_edit, self.snipe_react):
            d[ctx.channel.id].clear()
        await send(ctx, "🧽 Cleared snipe data for this channel.")

    @commands.command()
    @has_perms(manage_channels=True)
    async def revokefiles(self, ctx, state: str, channel: Optional[discord.TextChannel] = None):
        if state.lower() not in ("on", "off"):
            return await send(ctx, f"Usage: `{ctx.clean_prefix}revokefiles on|off #channel`")
        channel = channel or ctx.channel
        on = state.lower() == "on"
        ow = channel.overwrites_for(ctx.guild.default_role)
        ow.attach_files = False if on else None
        ow.embed_links = False if on else None
        await channel.set_permissions(ctx.guild.default_role, overwrite=ow)
        await send(ctx, f"📎 Files and embeds {'revoked in' if on else 'restored in'} {channel.mention}.")

    @commands.group(invoke_without_command=True)
    @has_perms(manage_expressions=True)
    async def sticker(self, ctx):
        p = ctx.clean_prefix
        await send(ctx, f"Usage: `{p}sticker add <name>` (attach an image or reply to one) "
                        f"or `{p}sticker add <image URL> <name>`")

    @sticker.command(name="add")
    @has_perms(manage_expressions=True)
    async def sticker_add(self, ctx, *, args: str):
        url, name = None, args.strip()
        first, _, rest = args.partition(" ")
        if first.startswith("http"):
            url, name = first, rest.strip()
        elif ctx.message.attachments:
            url = ctx.message.attachments[0].url
        elif ctx.message.reference and isinstance(ctx.message.reference.resolved, discord.Message):
            ref = ctx.message.reference.resolved
            if ref.attachments:
                url = ref.attachments[0].url
        if not url or not name:
            return await send(ctx, "I need a name plus an image (attachment, reply, or URL).")
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                data = await r.read()
        if len(data) > 512 * 1024:
            return await send(ctx, "Stickers must be under 512 KB.")
        file = discord.File(io.BytesIO(data), filename="sticker.png")
        st = await ctx.guild.create_sticker(name=name[:30], description=name[:100], emoji="🙂",
                                            file=file, reason=why(ctx, "sticker add"))
        await send(ctx, f"✅ Added sticker **{st.name}**.")

    @commands.group(invoke_without_command=True)
    @has_perms(manage_expressions=True)
    async def emoji(self, ctx):
        await send(ctx, f"Usage: `{ctx.clean_prefix}emoji addmany <custom emojis>` (up to 25)")

    @emoji.command(name="addmany")
    @has_perms(manage_expressions=True)
    async def emoji_addmany(self, ctx, *, emojis: str = ""):
        text = emojis
        if not text and ctx.message.reference and isinstance(ctx.message.reference.resolved, discord.Message):
            text = ctx.message.reference.resolved.content
        found = re.findall(r"<(a?):(\w+):(\d+)>", text)[:25]
        if not found:
            return await send(ctx, "Include custom emojis in the command, or reply to a message that has them.")
        added, failed = 0, []
        async with aiohttp.ClientSession() as s:
            for animated, name, eid in found:
                ext = "gif" if animated else "png"
                try:
                    async with s.get(f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=128") as r:
                        data = await r.read()
                    await ctx.guild.create_custom_emoji(name=name, image=data, reason=why(ctx, "emoji addmany"))
                    added += 1
                except discord.HTTPException:
                    failed.append(name)
        msg = f"✅ Added {added} emoji(s)."
        if failed:
            msg += f" Failed: {', '.join(failed)} (server may be out of slots)."
        await send(ctx, msg)

    # ======================================================================
    # MODERATION RECORDS
    # ======================================================================
    @commands.group(invoke_without_command=True)
    @has_perms(moderate_members=True)
    async def notes(self, ctx, member: discord.User):
        items = db["notes"].get(str(ctx.guild.id), {}).get(str(member.id), [])
        if not items:
            return await send(ctx, f"No notes for **{member}**.")
        lines = [f"`#{n['id']}` <t:{n['at']}:d> by <@{n['mod']}> — {n['text']}" for n in items]
        await send(ctx, embed=discord.Embed(title=f"Notes for {member}", description="\n".join(lines)[:4000]))

    @notes.command(name="add")
    @has_perms(moderate_members=True)
    async def notes_add(self, ctx, member: discord.User, *, note: str):
        nid = db["counters"].get("note", 0) + 1
        db["counters"]["note"] = nid
        db["notes"].setdefault(str(ctx.guild.id), {}).setdefault(str(member.id), []).append(
            {"id": nid, "mod": ctx.author.id, "text": note[:500], "at": int(time.time())})
        save()
        await send(ctx, f"📝 Added note `#{nid}` for **{member}**.")

    @notes.command(name="remove")
    @has_perms(moderate_members=True)
    async def notes_remove(self, ctx, member: discord.User, note_id: int):
        items = db["notes"].get(str(ctx.guild.id), {}).get(str(member.id), [])
        new = [n for n in items if n["id"] != note_id]
        if len(new) == len(items):
            return await send(ctx, "No note with that ID for that user.")
        db["notes"][str(ctx.guild.id)][str(member.id)] = new
        save()
        await send(ctx, f"🗑️ Removed note `#{note_id}`.")

    @notes.command(name="clear")
    @has_perms(moderate_members=True)
    async def notes_clear(self, ctx, member: discord.User):
        n = len(db["notes"].get(str(ctx.guild.id), {}).pop(str(member.id), []))
        save()
        await send(ctx, f"🧽 Cleared {n} note(s) for **{member}**.")

    @commands.group(invoke_without_command=True)
    @has_perms(moderate_members=True)
    async def history(self, ctx, user: Optional[discord.User] = None, action: Optional[str] = None):
        cases = db["cases"].get(str(ctx.guild.id), [])
        if user:
            cases = [c for c in cases if c["user"] == user.id]
        if action:
            cases = [c for c in cases if c["action"] == action.lower()]
        if not cases:
            return await send(ctx, "No cases found.")
        lines = [fmt_case(c) for c in reversed(cases[-15:])]
        title = f"History — {user}" if user else "Recent cases"
        await send(ctx, embed=discord.Embed(title=title, description="\n".join(lines)[:4000]))

    @history.command(name="view")
    @has_perms(moderate_members=True)
    async def history_view(self, ctx, case_id: int):
        c = next((c for c in db["cases"].get(str(ctx.guild.id), []) if c["id"] == case_id), None)
        if not c:
            return await send(ctx, "No such case.")
        e = discord.Embed(title=f"Case #{c['id']} — {c['action']}", description=c["reason"])
        e.add_field(name="User", value=f"<@{c['user']}> (`{c['user']}`)")
        e.add_field(name="Moderator", value=f"<@{c['mod']}>")
        e.add_field(name="When", value=f"<t:{c['at']}:F>")
        if c.get("duration"):
            e.add_field(name="Duration", value=c["duration"])
        await send(ctx, embed=e)

    @history.command(name="remove")
    @has_perms(manage_guild=True)
    async def history_remove(self, ctx, user: discord.User, case_id: int):
        cases = db["cases"].get(str(ctx.guild.id), [])
        new = [c for c in cases if not (c["id"] == case_id and c["user"] == user.id)]
        if len(new) == len(cases):
            return await send(ctx, "No matching case for that user.")
        db["cases"][str(ctx.guild.id)] = new
        save()
        await send(ctx, f"🗑️ Removed case `#{case_id}`.")

    @history.command(name="removeall")
    @has_perms(manage_guild=True)
    async def history_removeall(self, ctx, user: discord.User):
        cases = db["cases"].get(str(ctx.guild.id), [])
        new = [c for c in cases if c["user"] != user.id]
        db["cases"][str(ctx.guild.id)] = new
        save()
        await send(ctx, f"🗑️ Removed {len(cases) - len(new)} case(s) for **{user}**.")

    @commands.command()
    @has_perms(moderate_members=True)
    async def modstats(self, ctx, mod: Optional[discord.User] = None):
        mod = mod or ctx.author
        cases = [c for c in db["cases"].get(str(ctx.guild.id), []) if c["mod"] == mod.id]
        now = time.time()

        def n(days):
            return sum(1 for c in cases if now - c["at"] <= days * 86400)

        e = discord.Embed(title=f"Mod stats — {mod}")
        e.add_field(name="Last 7 days", value=n(7))
        e.add_field(name="Last 14 days", value=n(14))
        e.add_field(name="All time", value=len(cases))
        by = Counter(c["action"] for c in cases)
        if by:
            e.add_field(name="By action (all time)",
                        value=", ".join(f"{k}: {v}" for k, v in by.most_common()), inline=False)
        await send(ctx, embed=e)

    @commands.command()
    @has_perms(moderate_members=True)
    async def modhistory(self, ctx, mod: Optional[discord.User] = None):
        mod = mod or ctx.author
        cases = [c for c in db["cases"].get(str(ctx.guild.id), []) if c["mod"] == mod.id]
        if not cases:
            return await send(ctx, "No saved cases for that moderator.")
        lines = [fmt_case(c) for c in reversed(cases[-20:])]
        await send(ctx, embed=discord.Embed(title=f"Cases by {mod}", description="\n".join(lines)[:4000]))

    @commands.command()
    @has_perms(moderate_members=True)
    async def whois(self, ctx, target: Optional[discord.User] = None):
        user = target or ctx.author
        member = ctx.guild.get_member(user.id)
        gid = str(ctx.guild.id)
        e = discord.Embed(title=str(user))
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name="ID", value=user.id)
        e.add_field(name="Created", value=f"<t:{int(user.created_at.timestamp())}:R>")
        if member:
            if member.joined_at:
                e.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:R>")
            roles = [r.mention for r in reversed(member.roles) if r != ctx.guild.default_role]
            e.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles)[:1000] or "None", inline=False)
        else:
            try:
                ban = await ctx.guild.fetch_ban(user)
                e.add_field(name="Status", value=f"Banned — {ban.reason or 'no reason'}", inline=False)
            except (discord.NotFound, discord.Forbidden):
                e.add_field(name="Status", value="Not in this server", inline=False)
        e.add_field(name="Warnings", value=len(db["warns"].get(gid, {}).get(str(user.id), [])))
        e.add_field(name="Notes", value=len(db["notes"].get(gid, {}).get(str(user.id), [])))
        e.add_field(name="Cases", value=sum(1 for c in db["cases"].get(gid, []) if c["user"] == user.id))
        await send(ctx, embed=e)

    # ---- reminders ----------------------------------------------------------
    @commands.group(invoke_without_command=True)
    async def remind(self, ctx, duration: str, *, text: str):
        secs = parse_duration(duration)
        if not secs or secs > 365 * 86400:
            return await send(ctx, "Duration like `10m`, `2h`, `3d`.")
        rid = db["counters"].get("reminder", 0) + 1
        db["counters"]["reminder"] = rid
        db["reminders"].append({"id": rid, "user": ctx.author.id, "channel": ctx.channel.id,
                                "at": time.time() + secs, "text": text[:500]})
        save()
        await send(ctx, f"⏰ Okay, I'll remind you in {duration}. (ID `{rid}`)")

    @remind.command(name="list")
    async def remind_list(self, ctx):
        mine = [r for r in db["reminders"] if r["user"] == ctx.author.id]
        if not mine:
            return await send(ctx, "You have no reminders.")
        await send(ctx, "\n".join(f"`{r['id']}` <t:{int(r['at'])}:R> — {r['text']}" for r in mine)[:1900])

    @remind.command(name="remove")
    async def remind_remove(self, ctx, reminder_id: int):
        new = [r for r in db["reminders"] if not (r["id"] == reminder_id and r["user"] == ctx.author.id)]
        if len(new) == len(db["reminders"]):
            return await send(ctx, "No reminder of yours with that ID.")
        db["reminders"] = new
        save()
        await send(ctx, "🗑️ Reminder removed.")

    # ======================================================================
    # MUTE AND JAIL SETUP
    # ======================================================================
    @commands.command()
    @has_perms(manage_roles=True)
    async def setupmute(self, ctx):
        await send(ctx, "Setting up mute roles… (this can take a moment)")
        for name in MUTE_ROLES:
            await self.ensure_mute_role(ctx.guild, name, force=True)
        await send(ctx, "✅ Created/updated **Muted**, **Image Muted** and **Reaction Muted** with channel permissions.")

    async def apply_mute(self, ctx, member, rest, role_name, kind, label):
        secs, dlabel, reason = split_duration(rest)
        if not await self.guard(ctx, member):
            return
        role = await self.ensure_mute_role(ctx.guild, role_name)
        async with notify(ctx, member, kind, reason, dlabel):
            await member.add_roles(role, reason=why(ctx, reason))
        remove_timers("unmute", ctx.guild.id, member.id, role.id)
        if secs:
            add_timer("unmute", ctx.guild.id, member.id, secs, role.id)
        self.log_case(ctx, kind, member, reason, dlabel)
        await send(ctx, f"🤐 {label} **{member}**" + (f" for {dlabel}." if dlabel else "."))

    async def lift_mute(self, ctx, member, reason, role_name, kind, label):
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if not role or role not in member.roles:
            return await send(ctx, f"**{member}** doesn't have the {role_name} role.")
        await member.remove_roles(role, reason=why(ctx, reason))
        remove_timers("unmute", ctx.guild.id, member.id, role.id)
        self.log_case(ctx, kind, member, reason)
        await send(ctx, f"🔊 {label} **{member}**.")

    @commands.command(aliases=["stfu"])
    @has_perms(moderate_members=True)
    async def mute(self, ctx, member: discord.Member, *, rest: str = ""):
        await self.apply_mute(ctx, member, rest, "Muted", "mute", "Muted")

    @commands.command()
    @has_perms(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str = None):
        await self.lift_mute(ctx, member, reason, "Muted", "unmute", "Unmuted")

    @commands.command()
    @has_perms(moderate_members=True)
    async def imute(self, ctx, member: discord.Member, *, rest: str = ""):
        await self.apply_mute(ctx, member, rest, "Image Muted", "imute", "Image-muted")

    @commands.command()
    @has_perms(moderate_members=True)
    async def iunmute(self, ctx, member: discord.Member, *, reason: str = None):
        await self.lift_mute(ctx, member, reason, "Image Muted", "iunmute", "Image-unmuted")

    @commands.command()
    @has_perms(moderate_members=True)
    async def rmute(self, ctx, member: discord.Member, *, rest: str = ""):
        await self.apply_mute(ctx, member, rest, "Reaction Muted", "rmute", "Reaction-muted")

    @commands.command()
    @has_perms(moderate_members=True)
    async def runmute(self, ctx, member: discord.Member, *, reason: str = None):
        await self.lift_mute(ctx, member, reason, "Reaction Muted", "runmute", "Reaction-unmuted")

    @commands.command(aliases=["setupmod"])
    @has_perms(manage_guild=True)
    async def modsetup(self, ctx):
        g = ctx.guild
        role = await self.get_jail_role(g)
        staff = [r for r in g.roles if r.permissions.administrator or r.permissions.kick_members
                 or r.permissions.ban_members or r.permissions.manage_messages]
        jail_ch = discord.utils.get(g.text_channels, name="jail")
        if not jail_ch:
            jail_ch = await g.create_text_channel("jail", overwrites={
                g.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True)})
        log_ch = discord.utils.get(g.text_channels, name="jail-log")
        if not log_ch:
            ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                  g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
            for r in staff:
                ow[r] = discord.PermissionOverwrite(view_channel=True)
            log_ch = await g.create_text_channel("jail-log", overwrites=ow)
        db["config"].setdefault(str(g.id), {})["jail_log"] = log_ch.id
        save()
        await send(ctx, f"✅ Ready: {jail_ch.mention} (jailed members) and {log_ch.mention} (staff only).")

    # ======================================================================
    # COMMAND RESTRICTIONS (Manage Server)
    # ======================================================================
    @staticmethod
    def _resolve_cmd(bot, name):
        cmd = bot.get_command(name.lower())
        return (cmd.root_parent or cmd).name if cmd else None

    @commands.group(name="disablecommand", aliases=["dcmd"], invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def disablecommand(self, ctx, target: Target, *, command: str):
        name = self._resolve_cmd(self.bot, command)
        if not name:
            return await send(ctx, "Unknown command.")
        key = "members" if isinstance(target, discord.Member) else "channels"
        lst = dis_conf(ctx.guild.id)[key].setdefault(str(target.id), [])
        if name not in lst:
            lst.append(name)
        save()
        await send(ctx, f"🚫 `{name}` disabled for {target.mention}.")

    @disablecommand.command(name="all")
    @has_perms(manage_guild=True)
    async def disablecommand_all(self, ctx, *, command: str):
        name = self._resolve_cmd(self.bot, command)
        if not name:
            return await send(ctx, "Unknown command.")
        conf = dis_conf(ctx.guild.id)
        if name not in conf["all"]:
            conf["all"].append(name)
        save()
        await send(ctx, f"🚫 `{name}` disabled server-wide.")

    @disablecommand.command(name="list")
    @has_perms(manage_guild=True)
    async def disablecommand_list(self, ctx):
        conf = dis_conf(ctx.guild.id)
        lines = []
        if conf["all"]:
            lines.append("**Everywhere:** " + ", ".join(f"`{c}`" for c in conf["all"]))
        for cid, cmds in conf["channels"].items():
            if cmds:
                lines.append(f"<#{cid}>: " + ", ".join(f"`{c}`" for c in cmds))
        for uid, cmds in conf["members"].items():
            if cmds:
                lines.append(f"<@{uid}>: " + ", ".join(f"`{c}`" for c in cmds))
        await send(ctx, "\n".join(lines)[:1900] or "No disabled commands.")

    @commands.group(name="enablecommand", aliases=["ecmd"], invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def enablecommand(self, ctx, target: Target, *, command: str):
        name = self._resolve_cmd(self.bot, command)
        if not name:
            return await send(ctx, "Unknown command.")
        key = "members" if isinstance(target, discord.Member) else "channels"
        lst = dis_conf(ctx.guild.id)[key].get(str(target.id), [])
        if name in lst:
            lst.remove(name)
        save()
        await send(ctx, f"✅ `{name}` re-enabled for {target.mention}.")

    @enablecommand.command(name="all")
    @has_perms(manage_guild=True)
    async def enablecommand_all(self, ctx, *, command: str):
        name = self._resolve_cmd(self.bot, command)
        if not name:
            return await send(ctx, "Unknown command.")
        conf = dis_conf(ctx.guild.id)
        if name in conf["all"]:
            conf["all"].remove(name)
        for key in ("channels", "members"):
            for lst in conf[key].values():
                if name in lst:
                    lst.remove(name)
        save()
        await send(ctx, f"✅ Removed every block on `{name}`.")

    @commands.group(invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def ignore(self, ctx):
        p = ctx.clean_prefix
        await send(ctx, f"Usage: `{p}ignore add|remove @member|#channel` or `{p}ignore list`")

    @ignore.command(name="add")
    @has_perms(manage_guild=True)
    async def ignore_add(self, ctx, target: Union[discord.TextChannel, discord.Member]):
        key = "members" if isinstance(target, discord.Member) else "channels"
        lst = ign_conf(ctx.guild.id)[key]
        if target.id not in lst:
            lst.append(target.id)
        save()
        await send(ctx, f"🙉 Now ignoring commands from {target.mention}.")

    @ignore.command(name="remove")
    @has_perms(manage_guild=True)
    async def ignore_remove(self, ctx, target: Union[discord.TextChannel, discord.Member]):
        key = "members" if isinstance(target, discord.Member) else "channels"
        lst = ign_conf(ctx.guild.id)[key]
        if target.id in lst:
            lst.remove(target.id)
        save()
        await send(ctx, f"👂 No longer ignoring {target.mention}.")

    @ignore.command(name="list")
    @has_perms(manage_guild=True)
    async def ignore_list(self, ctx):
        c = ign_conf(ctx.guild.id)
        lines = [f"<#{i}>" for i in c["channels"]] + [f"<@{i}>" for i in c["members"]]
        await send(ctx, "**Ignored**\n" + "\n".join(lines) if lines else "Nothing is ignored.")

    async def _restrict(self, ctx, command, role, mode):
        name = self._resolve_cmd(self.bot, command)
        if not name:
            return await send(ctx, "Unknown command.")
        lst = restrict_conf(ctx.guild.id).setdefault(name, [])
        if mode == "toggle":
            mode = "remove" if role.id in lst else "add"
        if mode == "add" and role.id not in lst:
            lst.append(role.id)
        elif mode == "remove" and role.id in lst:
            lst.remove(role.id)
        if not lst:
            restrict_conf(ctx.guild.id).pop(name, None)
        save()
        verb = "can now use" if mode == "add" else "removed from"
        await send(ctx, f"🔐 **{role.name}** {verb} `{name}` restriction list. "
                        f"(Only listed roles can use a restricted command; admins always can.)")

    @commands.group(name="restrictcommand", aliases=["rc"], invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def restrictcommand(self, ctx, command: str, role: discord.Role):
        await self._restrict(ctx, command, role, "toggle")

    @restrictcommand.command(name="add")
    @has_perms(manage_guild=True)
    async def restrictcommand_add(self, ctx, command: str, role: discord.Role):
        await self._restrict(ctx, command, role, "add")

    @restrictcommand.command(name="remove")
    @has_perms(manage_guild=True)
    async def restrictcommand_remove(self, ctx, command: str, role: discord.Role):
        await self._restrict(ctx, command, role, "remove")

    @restrictcommand.command(name="list")
    @has_perms(manage_guild=True)
    async def restrictcommand_list(self, ctx, *, query: Optional[str] = None):
        conf = restrict_conf(ctx.guild.id)
        if query:
            try:
                role = await commands.RoleConverter().convert(ctx, query)
                conf = {c: ids for c, ids in conf.items() if role.id in ids}
            except commands.BadArgument:
                name = self._resolve_cmd(self.bot, query)
                conf = {c: ids for c, ids in conf.items() if c == name}
        if not conf:
            return await send(ctx, "No restrictions.")
        await send(ctx, "\n".join(f"`{c}` → " + ", ".join(f"<@&{i}>" for i in ids)
                                  for c, ids in conf.items())[:1900])

    @restrictcommand.command(name="reset")
    @has_perms(manage_guild=True)
    async def restrictcommand_reset(self, ctx):
        db["restrict"][str(ctx.guild.id)] = {}
        save()
        await send(ctx, "🧽 All command restrictions cleared.")

    # ======================================================================
    # AUTOMATIC MODERATION (filter)
    # ======================================================================
    def links_hit(self, text, conf, channel_id):
        urls = URL_RE.findall(text)
        if not urls:
            return False
        wl = conf.get("whitelist", {})
        allowed = set(wl.get(str(channel_id), [])) | set(wl.get("all", []))
        for u in urls:
            host = re.sub(r"^https?://", "", u, flags=re.I).split("/")[0].split("?")[0].split(":")[0].lower()
            host = host.removeprefix("www.")
            if not any(host == d or host.endswith("." + d) for d in allowed):
                return True
        return False

    def spam_hit(self, message, conf):
        thr = conf.get("threshold") or 5
        iv = conf.get("interval") or 5
        now = time.time()
        key = (message.guild.id, message.author.id)
        log = [(t, m) for t, m in self.spam_log.get(key, []) if now - t <= iv] + [(now, message)]
        self.spam_log[key] = log
        if len(log) >= thr:
            self.spam_log[key] = []
            for _, m in log[:-1]:
                asyncio.create_task(safe_delete(m))
            return True
        return False

    async def run_filters(self, message) -> bool:
        gconf = db["filters"].get(str(message.guild.id))
        if not gconf:
            return False
        member = message.author
        if member.guild_permissions.administrator:
            return False
        for ftype, conf in gconf.items():
            if ftype not in FILTER_TYPES:
                continue
            if not (conf["all"] or message.channel.id in conf["channels"]):
                continue
            if any(r.id in conf["exempt"] for r in member.roles):
                continue
            text, hit = message.content, False
            if ftype == "links":
                hit = self.links_hit(text, conf, message.channel.id)
            elif ftype == "invites":
                hit = bool(INVITE_RE.search(text))
            elif ftype == "caps":
                letters = [c for c in text if c.isalpha()]
                upper = sum(c.isupper() for c in letters)
                hit = upper >= (conf.get("threshold") or 10) and upper / max(len(letters), 1) >= 0.6
            elif ftype == "spam":
                hit = self.spam_hit(message, conf)
            if hit:
                await safe_delete(message)
                try:
                    await message.channel.send(f"{member.mention} that's not allowed here ({ftype} filter).",
                                               delete_after=5,
                                               allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException:
                    pass
                return True
        return False

    async def get_keyword_rule(self, guild):
        for r in await guild.fetch_automod_rules():
            if r.name == RULE_NAME:
                return r
        return None

    async def set_keywords(self, guild, words):
        rule = await self.get_keyword_rule(guild)
        if not words:
            if rule:
                await rule.delete(reason="No keywords left")
            return
        trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword,
                                         keyword_filter=words[:1000])
        if rule:
            await rule.edit(trigger=trigger, reason="Keyword update")
        else:
            await guild.create_automod_rule(
                name=RULE_NAME,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=[discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled=True, reason="Keyword filter")

    async def filter_extras(self, ctx, ftype, rest) -> bool:
        """filter links whitelist / snipe / add / remove / list / wordmigrate."""
        gid = str(ctx.guild.id)
        p = ctx.clean_prefix
        tokens = rest.split()

        if ftype == "links" and tokens and tokens[0].lower() == "whitelist":
            if len(tokens) < 3:
                await send(ctx, f"Usage: `{p}filter links whitelist #channel|all <domain>`")
                return True
            if tokens[1].lower() == "all":
                key = "all"
            else:
                try:
                    key = str((await commands.TextChannelConverter().convert(ctx, tokens[1])).id)
                except commands.BadArgument:
                    await send(ctx, "Channel not found.")
                    return True
            domain = (tokens[2].lower().removeprefix("https://").removeprefix("http://")
                      .removeprefix("www.").split("/")[0])
            lst = filt_conf(gid, "links").setdefault("whitelist", {}).setdefault(key, [])
            if domain in lst:
                lst.remove(domain)
                msg = f"❌ `{domain}` removed from the link whitelist."
            else:
                lst.append(domain)
                msg = f"✅ `{domain}` is now whitelisted for links."
            save()
            await send(ctx, msg)
            return True

        if ftype == "snipe":
            conf = db["snipe_filters"].setdefault(gid, {"types": [], "words": []})
            if not tokens:
                await send(ctx, f"Usage: `{p}filter snipe {'|'.join(SNIPE_TYPES)}` or `{p}filter snipe word <text>`\n"
                                f"Blocked types: {', '.join(conf['types']) or 'none'} · "
                                f"words: {', '.join(conf['words']) or 'none'}")
                return True
            if tokens[0].lower() == "word":
                word = " ".join(tokens[1:]).lower()
                if not word:
                    await send(ctx, "Give me a word or phrase.")
                    return True
                if word in conf["words"]:
                    conf["words"].remove(word)
                    msg = f"❌ `{word}` is no longer hidden from snipe."
                else:
                    conf["words"].append(word)
                    msg = f"✅ Snipe will now skip messages containing `{word}`."
            elif tokens[0].lower() in SNIPE_TYPES:
                t = tokens[0].lower()
                if t in conf["types"]:
                    conf["types"].remove(t)
                    msg = f"❌ Snipe will show **{t}** again."
                else:
                    conf["types"].append(t)
                    msg = f"✅ Snipe will now skip messages with **{t}**."
            else:
                await send(ctx, f"Pick one of: {', '.join(SNIPE_TYPES)}, or `word <text>`.")
                return True
            save()
            await send(ctx, msg)
            return True

        if ftype in ("add", "remove", "list", "wordmigrate"):
            try:
                rule = await self.get_keyword_rule(ctx.guild)
                current = list(rule.trigger.keyword_filter) if rule else []
                if ftype == "list":
                    await send(ctx, "**AutoMod keywords**\n"
                               + (", ".join(f"`{w}`" for w in current)[:1900] or "None."))
                    return True
                if ftype == "wordmigrate":
                    legacy = db["legacy_words"].pop(gid, [])
                    if not legacy:
                        await send(ctx, "No legacy local keywords found to import.")
                        return True
                    await self.set_keywords(ctx.guild, sorted(set(current) | set(legacy)))
                    save()
                    await send(ctx, f"✅ Imported {len(legacy)} legacy keyword(s) into AutoMod.")
                    return True
                words = split_words(rest)
                if not words:
                    await send(ctx, f"Usage: `{p}filter {ftype} <word, phrase>`")
                    return True
                if ftype == "add":
                    await self.set_keywords(ctx.guild, sorted(set(current) | set(words)))
                    await send(ctx, f"✅ Added {len(words)} keyword(s) to AutoMod.")
                else:
                    await self.set_keywords(ctx.guild, [w for w in current if w not in words])
                    await send(ctx, f"🗑️ Removed {len(words)} keyword(s) from AutoMod.")
            except discord.HTTPException as e:
                await send(ctx, f"Discord rejected that AutoMod change: `{e.text}`")
            return True
        return False

    @commands.command(name="filter")
    @has_perms(manage_guild=True)
    async def filter_cmd(self, ctx, ftype: Optional[str] = None, *, rest: str = ""):
        p = ctx.clean_prefix
        usage = (f"Usage: `{p}filter spam|links|caps|invites <#channel/all> <on/off> "
                 f"[--threshold N] [--interval N]` or `{p}filter <type> exempt @role`")
        gid = str(ctx.guild.id)
        if ftype is None:
            lines = []
            for t in FILTER_TYPES:
                c = db["filters"].get(gid, {}).get(t)
                if c and (c["all"] or c["channels"]):
                    where = "everywhere" if c["all"] else ", ".join(f"<#{i}>" for i in c["channels"])
                    lines.append(f"**{t}**: {where}")
            return await send(ctx, "\n".join(lines) or usage)
        ftype = ftype.lower()
        if await self.filter_extras(ctx, ftype, rest):
            return
        if ftype not in FILTER_TYPES:
            return await send(ctx, usage)
        conf = filt_conf(gid, ftype)
        tokens = rest.split()
        if tokens and tokens[0].lower() == "exempt":
            if len(tokens) < 2:
                return await send(ctx, usage)
            try:
                role = await commands.RoleConverter().convert(ctx, " ".join(tokens[1:]))
            except commands.BadArgument:
                return await send(ctx, "Role not found.")
            if role.id in conf["exempt"]:
                conf["exempt"].remove(role.id)
                msg = f"**{role.name}** is no longer exempt from the {ftype} filter."
            else:
                conf["exempt"].append(role.id)
                msg = f"**{role.name}** is now exempt from the {ftype} filter."
            save()
            return await send(ctx, msg)
        if len(tokens) < 2 or tokens[1].lower() not in ("on", "off"):
            return await send(ctx, usage)
        on = tokens[1].lower() == "on"
        i = 2
        while i < len(tokens):
            if tokens[i] in ("--threshold", "--interval") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
                conf[tokens[i][2:]] = max(1, int(tokens[i + 1]))
                i += 2
            else:
                i += 1
        if tokens[0].lower() == "all":
            conf["all"] = on
            if not on:
                conf["channels"] = []
            where = "the whole server"
        else:
            try:
                ch = await commands.TextChannelConverter().convert(ctx, tokens[0])
            except commands.BadArgument:
                return await send(ctx, "Channel not found. " + usage)
            if on and ch.id not in conf["channels"]:
                conf["channels"].append(ch.id)
            if not on and ch.id in conf["channels"]:
                conf["channels"].remove(ch.id)
            where = ch.mention
        save()
        extra = ""
        if ftype == "spam":
            extra = f" ({conf.get('threshold') or 5} msgs / {conf.get('interval') or 5}s)"
        elif ftype == "caps":
            extra = f" (threshold {conf.get('threshold') or 10})"
        await send(ctx, f"🛡️ {ftype} filter **{'on' if on else 'off'}** for {where}{extra}.")

    # ======================================================================
    # AUTOROLE
    # ======================================================================
    @commands.group(invoke_without_command=True)
    @has_perms(manage_roles=True)
    async def autorole(self, ctx):
        p = ctx.clean_prefix
        await send(ctx, f"**Autorole** — roles given to new members\n"
                        f"`{p}autorole add|remove @role`\n`{p}autorole list`\n`{p}autorole reset`")

    @autorole.command(name="add")
    @has_perms(manage_roles=True)
    async def autorole_add(self, ctx, *, role: discord.Role):
        if not await self.role_ok(ctx, role):
            return
        lst = db["autoroles"].setdefault(str(ctx.guild.id), [])
        if role.id not in lst:
            lst.append(role.id)
        save()
        await send(ctx, f"✅ New members will get **{role.name}**.")

    @autorole.command(name="remove")
    @has_perms(manage_roles=True)
    async def autorole_remove(self, ctx, *, role: discord.Role):
        lst = db["autoroles"].setdefault(str(ctx.guild.id), [])
        if role.id in lst:
            lst.remove(role.id)
        save()
        await send(ctx, f"✅ **{role.name}** removed from autoroles.")

    @autorole.command(name="list")
    @has_perms(manage_roles=True)
    async def autorole_list(self, ctx):
        ids = db["autoroles"].get(str(ctx.guild.id), [])
        await send(ctx, "**Autoroles**\n" + "\n".join(f"<@&{i}>" for i in ids) if ids else "No autoroles set.")

    @autorole.command(name="reset")
    @has_perms(manage_roles=True)
    async def autorole_reset(self, ctx):
        db["autoroles"][str(ctx.guild.id)] = []
        save()
        await send(ctx, "🧽 Autoroles cleared.")

    # ======================================================================
    # ANTI-RAID
    # ======================================================================
    @commands.group(invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def antiraid(self, ctx):
        p = ctx.clean_prefix
        await send(ctx,
                   f"**Anti-raid**\n"
                   f"`{p}antiraid newaccounts on|off [days]` — block new accounts\n"
                   f"`{p}antiraid avatar on|off` — require an avatar\n"
                   f"`{p}antiraid massjoin on|off [count] [seconds]` — detect mass joins\n"
                   f"`{p}antiraid whitelist <user>` / `whitelist view` — bypass list\n"
                   f"`{p}antiraid config` / `raidstate` — status")

    @antiraid.command(name="newaccounts")
    @has_perms(manage_guild=True)
    async def antiraid_newaccounts(self, ctx, state: str, days: Optional[int] = None):
        c = ar_conf(ctx.guild.id)
        c["newaccounts"]["on"] = parse_on(state)
        if days:
            c["newaccounts"]["days"] = max(1, days)
        save()
        await send(ctx, f"🛡️ New-account block **{'on' if c['newaccounts']['on'] else 'off'}** "
                        f"(min age {c['newaccounts']['days']} day(s)).")

    @antiraid.command(name="avatar")
    @has_perms(manage_guild=True)
    async def antiraid_avatar(self, ctx, state: str):
        c = ar_conf(ctx.guild.id)
        c["avatar"] = parse_on(state)
        save()
        await send(ctx, f"🛡️ Avatar requirement **{'on' if c['avatar'] else 'off'}**.")

    @antiraid.command(name="massjoin")
    @has_perms(manage_guild=True)
    async def antiraid_massjoin(self, ctx, state: str, count: Optional[int] = None,
                                seconds: Optional[int] = None):
        c = ar_conf(ctx.guild.id)
        c["massjoin"]["on"] = parse_on(state)
        if count:
            c["massjoin"]["count"] = max(2, count)
        if seconds:
            c["massjoin"]["seconds"] = max(1, seconds)
        save()
        mj = c["massjoin"]
        await send(ctx, f"🛡️ Mass-join detection **{'on' if mj['on'] else 'off'}** "
                        f"({mj['count']} joins / {mj['seconds']}s).")

    @antiraid.group(name="whitelist", invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def antiraid_whitelist(self, ctx, user: discord.User):
        wl = ar_conf(ctx.guild.id)["whitelist"]
        if user.id in wl:
            wl.remove(user.id)
            msg = f"❌ **{user}** removed from the anti-raid bypass list."
        else:
            wl.append(user.id)
            msg = f"✅ **{user}** can bypass anti-raid checks."
        save()
        await send(ctx, msg)

    @antiraid_whitelist.command(name="view")
    @has_perms(manage_guild=True)
    async def antiraid_whitelist_view(self, ctx):
        wl = ar_conf(ctx.guild.id)["whitelist"]
        await send(ctx, "**Anti-raid bypass list**\n" + "\n".join(f"<@{i}> (`{i}`)" for i in wl)
                   if wl else "The bypass list is empty.")

    @antiraid.command(name="config", aliases=["raidstate"])
    @has_perms(manage_guild=True)
    async def antiraid_config(self, ctx):
        c = ar_conf(ctx.guild.id)
        na, mj = c["newaccounts"], c["massjoin"]
        left = int(c.get("raid_until", 0) - time.time())
        e = discord.Embed(title="Anti-raid status")
        e.add_field(name="New accounts", value=f"{'on' if na['on'] else 'off'} (< {na['days']}d)")
        e.add_field(name="Avatar required", value="on" if c["avatar"] else "off")
        e.add_field(name="Mass join", value=f"{'on' if mj['on'] else 'off'} ({mj['count']}/{mj['seconds']}s)")
        e.add_field(name="Bypass list", value=len(c["whitelist"]))
        e.add_field(name="Raid state", value=f"🚨 active, {left}s left" if left > 0 else "calm")
        await send(ctx, embed=e)

    # ======================================================================
    # FAKE PERMISSIONS (virtual permissions only this bot honours)
    # ======================================================================
    def _fp_lines(self, ctx):
        conf = db["fakeperms"].get(str(ctx.guild.id), {})
        return [f"{'<@&' if k.startswith('role') else '<@'}{k.split(':')[1]}> — {', '.join(v)}"
                for k, v in conf.items() if v]

    @commands.group(name="fakepermissions", aliases=["fp"], invoke_without_command=True)
    @has_perms(manage_guild=True)
    async def fakepermissions(self, ctx):
        p = ctx.clean_prefix
        lines = self._fp_lines(ctx)
        await send(ctx,
                   f"**Virtual permissions** — bot-only; they don't change Discord permissions.\n"
                   f"`{p}fp grant <role> <perm, perm...>` · `{p}fp revoke @role_or_user <perm>` · "
                   f"`{p}fp list [@role_or_user]` · `{p}fp reset`\n"
                   f"**Current grants**\n" + ("\n".join(lines) if lines else "None") +
                   "\n\n**Valid permission names**\n" + ", ".join(f"`{x}`" for x in sorted(VALID_PERMS))[:1200])

    @fakepermissions.command(name="grant")
    @owner_or_admin()
    async def fp_grant(self, ctx, *, args: str):
        toks, perms = args.split(), []
        while toks:
            parts = [x for x in toks[-1].split(",") if x]
            if parts and all(x.lower() in VALID_PERMS for x in parts):
                perms = [x.lower() for x in parts] + perms
                toks.pop()
            else:
                break
        if not toks or not perms:
            return await send(ctx, f"Usage: `{ctx.clean_prefix}fp grant <role name/ID/mention> <permission, permission...>`")
        try:
            role = await commands.RoleConverter().convert(ctx, " ".join(toks))
        except commands.BadArgument:
            return await send(ctx, "Role not found (or a permission name is misspelled).")
        lst = db["fakeperms"].setdefault(str(ctx.guild.id), {}).setdefault(fp_key(role), [])
        for x in perms:
            if x not in lst:
                lst.append(x)
        save()
        await send(ctx, f"✅ **{role.name}** now has virtual: {', '.join(f'`{x}`' for x in perms)}")

    @fakepermissions.command(name="revoke")
    @owner_or_admin()
    async def fp_revoke(self, ctx, target: Union[discord.Role, discord.Member], permission: str):
        perm = permission.lower().strip(",")
        lst = db["fakeperms"].get(str(ctx.guild.id), {}).get(fp_key(target), [])
        if perm not in lst:
            return await send(ctx, "They don't have that virtual permission.")
        lst.remove(perm)
        save()
        await send(ctx, f"🗑️ Revoked `{perm}` from {target.mention}.")

    @fakepermissions.command(name="list")
    @has_perms(manage_guild=True)
    async def fp_list(self, ctx, target: Optional[Union[discord.Role, discord.Member]] = None):
        if target:
            perms = db["fakeperms"].get(str(ctx.guild.id), {}).get(fp_key(target), [])
            return await send(ctx, f"{target.mention}: "
                              + (", ".join(f"`{x}`" for x in perms) or "no virtual permissions"))
        await send(ctx, "\n".join(self._fp_lines(ctx))[:1900] or "No virtual permissions granted.")

    @fakepermissions.command(name="reset")
    @owner_or_admin()
    async def fp_reset(self, ctx):
        db["fakeperms"][str(ctx.guild.id)] = {}
        save()
        await send(ctx, "🧽 All virtual permissions cleared.")

    # ======================================================================
    # OWNER / HIGH-RISK
    # ======================================================================
    @commands.command()
    @nuke_allowed()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def nuke(self, ctx):
        """Delete and recreate THIS channel (same name, permissions, category and position)
        to wipe every message in it. Server owner, Anti-Nuke admins and Administrators only."""
        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel,
                                    discord.StageChannel, discord.ForumChannel)):
            return await send(ctx, "I can only nuke regular server channels.")

        confirm = discord.Embed(
            title="Nuke this channel?",
            description=f"You're about to nuke {channel.mention}. This permanently deletes the "
                        f"channel and recreates it empty, wiping every message in it.\n\n"
                        f"Type **yes** to nuke it or **cancel** to stop. "
                        f"This expires in 30 seconds.",
            color=discord.Color.red())
        await send(ctx, embed=confirm)

        def answered(m):
            return (m.author.id == ctx.author.id and m.channel.id == channel.id
                    and m.content.strip().lower() in ("yes", "cancel"))

        try:
            reply = await self.bot.wait_for("message", timeout=30, check=answered)
        except asyncio.TimeoutError:
            ctx.command.reset_cooldown(ctx)
            return await send(ctx, "Nuke timed out, nothing was changed.")
        if reply.content.strip().lower() == "cancel":
            ctx.command.reset_cooldown(ctx)
            return await send(ctx, "Nuke cancelled.")

        try:
            new = await channel.clone(reason=f"nuke by {ctx.author} ({ctx.author.id})")
            await new.edit(position=channel.position)
            await channel.delete(reason=f"nuke by {ctx.author} ({ctx.author.id})")
        except discord.Forbidden:
            return await send(ctx, "I'm missing permissions (Manage Channels) or my role is too low.")
        except discord.HTTPException as e:
            return await send(ctx, f"Nuke failed: `{e}`")

        if isinstance(new, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
            await new.send("first")


# --------------------------------------------------------------------------
# Extension entry point
# --------------------------------------------------------------------------
async def setup(bot):
    """Registers the cog only after the bot is ready, i.e. after every other cog has loaded.
    That way this file can stay named moderation.py and still never steal a command name or
    alias from another cog (those are skipped / dropped silently)."""
    cog = Moderation(bot)

    async def register():
        try:
            await bot.wait_until_ready()
            taken = bot.all_commands

            # Names already used by another cog: skip those commands instead of failing the whole cog.
            clash = [c.name for c in cog.get_commands() if c.name in taken]
            if clash:
                cog.__cog_commands__ = tuple(c for c in cog.__cog_commands__ if c.name not in taken)

            # Aliases already used elsewhere: drop just the alias, keep the command.
            for cmd in cog.get_commands():
                keep = [a for a in cmd.aliases if a not in taken]
                if len(keep) != len(cmd.aliases):
                    dropped = [a for a in cmd.aliases if a in taken]
                    cmd.aliases = keep

            await bot.add_cog(cog)
        except Exception:
            traceback.print_exc()

    bot._moderation_register_task = asyncio.create_task(register())