import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

from utils.store import GuildStore

COLOR = 0x0F0F10
A = discord.AuditLogAction

# module key -> description
MODULES = {
    "webhook": "Prevent mass webhook actions",
    "ban": "Prevent mass member bans",
    "kick": "Prevent mass member kicks",
    "channel": "Protect channels",
    "vanity": "Protect the server vanity",
    "role": "Protect roles",
    "botadd": "Prevent unauthorized bot additions",
    "emoji": "Prevent mass emoji and sticker deletion",
    "unban": "Prevent mass unbans",
    "prune": "Prevent member prunes",
    "integration": "Prevent unauthorized integrations",
}

# modules that use a "too many actions inside the window" limit
LIMIT_ACTIONS = ("ban", "kick", "channel", "role", "webhook", "emoji", "unban")

PUNISHMENTS = ("ban", "kick", "strip")

DEFAULTS = {
    "modules": {key: True for key in MODULES},
    "admins": [],
    "whitelist": [],
    "botwhitelist": [],
    "punishment": "ban",
    "logchannel": None,
    "window": 10,
    "limits": {"ban": 3, "kick": 3, "channel": 3, "role": 3, "webhook": 3, "emoji": 3, "unban": 3},
    "perm_grant": [],
    "perm_remove": [],
    "violations": [],
}

MAX_VIOLATIONS = 25

# actions that are punished immediately (no counting) when their module is on
INSTANT = {
    A.member_prune: ("prune", "ran a member prune"),
    A.integration_create: ("integration", "added an integration"),
}

# audit log action -> module it counts towards
COUNTED = {
    A.ban: "ban",
    A.kick: "kick",
    A.channel_create: "channel",
    A.channel_delete: "channel",
    A.role_create: "role",
    A.role_delete: "role",
    A.webhook_create: "webhook",
    A.emoji_delete: "emoji",
    A.sticker_delete: "emoji",
    A.unban: "unban",
}

ALL_PERMISSIONS = {name for name, _ in discord.Permissions.all()}


def parse_state(value):
    if value is None:
        return None
    v = value.lower()
    if v in ("on", "enable", "enabled", "true", "yes", "1"):
        return True
    if v in ("off", "disable", "disabled", "false", "no", "0"):
        return False
    return None


def is_owner():
    async def predicate(ctx: commands.Context):
        if ctx.guild and ctx.author.id == ctx.guild.owner_id:
            return True
        raise commands.CheckFailure("Only the server owner can use this command.")

    return commands.check(predicate)


def is_trusted():
    """Server owner or an Anti-Nuke admin."""

    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            return False
        if ctx.author.id == ctx.guild.owner_id:
            return True
        if ctx.author.id in ctx.cog.cfg(ctx.guild)["admins"]:
            return True
        raise commands.CheckFailure("Only the server owner or an Anti-Nuke admin can use this command.")

    return commands.check(predicate)


def mention_list(ids) -> str:
    return "\n".join(f"<@{i}> (`{i}`)" for i in ids) if ids else "None"


class AntiNuke(commands.Cog, name="AntiNuke"):
    """Protection modules and status commands"""

    help_text = """# Anti-Nuke
Anti-Nuke is always enabled. Individual module settings remain configurable.

## Module Settings (Server Owner or Anti-Nuke trust)
`{p}antinuke webhook <on/off>` - Prevent mass webhook actions
`{p}antinuke ban <on/off>` - Prevent mass member bans
`{p}antinuke kick <on/off>` - Prevent mass member kicks
`{p}antinuke channel <on/off>` - Protect channels
`{p}antinuke vanity <on/off>` - Protect the server vanity
`{p}antinuke role <on/off>` - Protect roles
`{p}antinuke botadd <on/off>` - Prevent unauthorized bot additions
`{p}antinuke emoji <on/off>` - Prevent mass emoji and sticker deletion
`{p}antinuke unban <on/off>` - Prevent mass unbans
`{p}antinuke prune <on/off>` - Prevent member prunes
`{p}antinuke integration <on/off>` - Prevent unauthorized integrations
`{p}antinuke all <on/off>` - Turn every module on or off at once

## Information (Anti-Nuke Admin)
`{p}antinuke config` - View server configuration
`{p}antinuke admins` - View Anti-Nuke Admins
`{p}antinuke admin list` - View Anti-Nuke Admins
`{p}antinuke list` - View modules and whitelisted users
`{p}an whitelistlist` / `{p}wls` - List whitelisted users
`{p}antinuke limits` - View action limits
`{p}antinuke health` - Check permissions and hierarchy
`{p}antinuke status` - Quick overview of protection state
`{p}antinuke violations [count]` - Recent Anti-Nuke violations
`{p}antinuke check @user` - See a user's Anti-Nuke standing
`{p}antinuke botwls` - List whitelisted bots
`{p}antinuke test` - Send a test message to the log channel

## Anti-Nuke Administration
`{p}an whitelist @user` / `{p}wl @user` - Toggle whitelist status
`{p}antinuke whitelist add @user` - Add to whitelist
`{p}antinuke whitelist remove @user` - Remove from whitelist
`{p}an whitelistlist` / `{p}wls` - List whitelisted users
`{p}antinuke whitelist clear confirm` - Empty the whitelist

## Server Owner
`{p}antinuke admin @user` - Add an Anti-Nuke Admin
`{p}antinuke admin add @user` - Add an Anti-Nuke Admin
`{p}antinuke admin remove @user` - Remove an Anti-Nuke Admin
`{p}antinuke admin clear confirm` - Remove every Anti-Nuke Admin

## Server Owner Settings
`{p}antinuke punishment <ban/kick/strip>` - Set punishment
`{p}antinuke logchannel [#channel]` - Set violation logs
`{p}antinuke logclear` - Stop logging violations
`{p}antinuke window <3-60>` - Set detection window
`{p}antinuke limit <action> <1-20>` - Change an action limit
`{p}antinuke permissions grant <permission>` - Watch permission grants
`{p}antinuke permissions remove <permission>` - Watch permission removals
`{p}antinuke permissions list` - Show watched permissions
`{p}antinuke permissions clear` - Stop watching all permissions
`{p}antinuke reset confirm` - Reset Anti-Nuke settings"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GuildStore("antinuke", DEFAULTS)
        self.hits = defaultdict(deque)  # (guild, user, module) -> timestamps
        self.recent = {}  # (guild, user) -> last punish time

    # ------------------------------------------------------------------ helpers

    def cfg(self, guild: discord.Guild) -> dict:
        return self.store.get(guild.id)

    def exempt(self, guild: discord.Guild, user_id: int, cfg: dict) -> bool:
        return (
            user_id == guild.owner_id
            or user_id == self.bot.user.id
            or user_id in cfg["whitelist"]
        )

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error) or "You can't use that command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}"
            await ctx.send(f"Usage: `{usage}`")
        else:
            raise error

    async def log(self, guild: discord.Guild, cfg: dict, embed: discord.Embed):
        channel_id = cfg["logchannel"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def punish(self, guild: discord.Guild, user_id: int, cfg: dict, reason: str):
        key = (guild.id, user_id)
        now = time.monotonic()
        if now - self.recent.get(key, -999.0) < 30:
            return  # already handled this user a moment ago
        self.recent[key] = now

        mode = cfg["punishment"]
        result = "Punished"
        history = cfg["violations"]
        history.append({"user": user_id, "reason": reason, "mode": mode, "time": int(time.time())})
        del history[:-MAX_VIOLATIONS]
        self.store.save()
        try:
            if mode == "ban":
                await guild.ban(discord.Object(id=user_id), reason=reason)
            elif mode == "kick":
                await guild.kick(discord.Object(id=user_id), reason=reason)
            else:  # strip
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                keep = [
                    r
                    for r in member.roles
                    if not r.is_default() and (r.managed or r >= guild.me.top_role)
                ]
                await member.edit(roles=keep, reason=reason)
        except discord.Forbidden:
            result = "Failed: missing permissions or role hierarchy (run health)"
        except discord.HTTPException as e:
            result = f"Failed: {e}"

        embed = discord.Embed(title="Anti-Nuke violation", color=COLOR)
        embed.add_field(name="User", value=f"<@{user_id}> (`{user_id}`)", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Punishment", value=f"{mode} - {result}", inline=False)
        await self.log(guild, cfg, embed)

    # --------------------------------------------------------------- detection

    async def _count(self, entry: discord.AuditLogEntry, cfg: dict, module: str):
        guild = entry.guild
        if not cfg["modules"][module]:
            return
        uid = entry.user_id
        if self.exempt(guild, uid, cfg):
            return

        now = time.monotonic()
        window = cfg["window"]
        limit = cfg["limits"][module]
        hits = self.hits[(guild.id, uid, module)]
        hits.append(now)
        while hits and now - hits[0] > window:
            hits.popleft()

        if len(hits) > limit:
            hits.clear()
            await self.punish(
                guild, uid, cfg, f"Anti-Nuke: more than {limit} {module} actions in {window}s"
            )

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        uid = entry.user_id
        if uid is None or uid == self.bot.user.id:
            return
        cfg = self.cfg(guild)
        action = entry.action

        if action in COUNTED:
            await self._count(entry, cfg, COUNTED[action])

        elif action in INSTANT:
            module, what = INSTANT[action]
            if cfg["modules"][module] and not self.exempt(guild, uid, cfg):
                await self.punish(guild, uid, cfg, f"Anti-Nuke: {what}")

        elif action == A.bot_add and cfg["modules"]["botadd"]:
            if self.exempt(guild, uid, cfg):
                return
            bot_id = getattr(entry.target, "id", None)
            if bot_id in cfg["botwhitelist"]:
                return  # this specific bot is allowed no matter who added it
            if bot_id:
                try:
                    await guild.kick(discord.Object(id=bot_id), reason="Anti-Nuke: unauthorized bot")
                except discord.HTTPException:
                    pass
            await self.punish(guild, uid, cfg, "Anti-Nuke: added a bot without being whitelisted")

        elif action == A.guild_update and cfg["modules"]["vanity"]:
            changed = hasattr(entry.before, "vanity_url_code") or hasattr(
                entry.after, "vanity_url_code"
            )
            # fall back to the raw change list in case the diff doesn't expose it
            changed = changed or any(
                c.get("key") == "vanity_url_code" for c in getattr(entry, "_changes", [])
            )
            if changed and not self.exempt(guild, uid, cfg):
                await self.punish(guild, uid, cfg, "Anti-Nuke: changed the server vanity")

        elif action == A.role_update:
            before = getattr(entry.before, "permissions", None)
            after = getattr(entry.after, "permissions", None)
            if before is None or after is None or self.exempt(guild, uid, cfg):
                return
            gained = [n for n, v in after if v and not getattr(before, n, False)]
            lost = [n for n, v in before if v and not getattr(after, n, False)]
            bad_grant = [n for n in gained if n in cfg["perm_grant"]]
            bad_remove = [n for n in lost if n in cfg["perm_remove"]]
            if bad_grant:
                await self.punish(
                    guild, uid, cfg, f"Anti-Nuke: granted watched permission(s): {', '.join(bad_grant)}"
                )
            elif bad_remove:
                await self.punish(
                    guild, uid, cfg, f"Anti-Nuke: removed watched permission(s): {', '.join(bad_remove)}"
                )

    # ---------------------------------------------------------------- commands

    @commands.group(name="antinuke", aliases=["an"], invoke_without_command=True)
    async def antinuke(self, ctx: commands.Context):
        """Anti-Nuke protection. See the help menu for every subcommand."""
        await ctx.send(
            f"Anti-Nuke is always on. See `{ctx.prefix}help antinuke` for the full command list."
        )

    # ---- module toggles (owner or trusted)

    async def _set_module(self, ctx: commands.Context, key: str, state):
        cfg = self.cfg(ctx.guild)
        new = parse_state(state)
        if new is None:
            current = "on" if cfg["modules"][key] else "off"
            return await ctx.send(
                f"`{key}` is currently **{current}**. Usage: `{ctx.prefix}antinuke {key} <on/off>`"
            )
        cfg["modules"][key] = new
        self.store.save()
        await ctx.send(f"Anti-Nuke **{key}** protection is now **{'on' if new else 'off'}**.")

    @antinuke.command(name="webhook")
    @is_trusted()
    async def an_webhook(self, ctx: commands.Context, state: str = None):
        """Prevent mass webhook actions"""
        await self._set_module(ctx, "webhook", state)

    @antinuke.command(name="ban")
    @is_trusted()
    async def an_ban(self, ctx: commands.Context, state: str = None):
        """Prevent mass member bans"""
        await self._set_module(ctx, "ban", state)

    @antinuke.command(name="kick")
    @is_trusted()
    async def an_kick(self, ctx: commands.Context, state: str = None):
        """Prevent mass member kicks"""
        await self._set_module(ctx, "kick", state)

    @antinuke.command(name="channel")
    @is_trusted()
    async def an_channel(self, ctx: commands.Context, state: str = None):
        """Protect channels"""
        await self._set_module(ctx, "channel", state)

    @antinuke.command(name="vanity")
    @is_trusted()
    async def an_vanity(self, ctx: commands.Context, state: str = None):
        """Protect the server vanity"""
        await self._set_module(ctx, "vanity", state)

    @antinuke.command(name="role")
    @is_trusted()
    async def an_role(self, ctx: commands.Context, state: str = None):
        """Protect roles"""
        await self._set_module(ctx, "role", state)

    @antinuke.command(name="botadd")
    @is_trusted()
    async def an_botadd(self, ctx: commands.Context, state: str = None):
        """Prevent unauthorized bot additions"""
        await self._set_module(ctx, "botadd", state)

    @antinuke.command(name="emoji")
    @is_trusted()
    async def an_emoji(self, ctx: commands.Context, state: str = None):
        """Prevent mass emoji and sticker deletion"""
        await self._set_module(ctx, "emoji", state)

    @antinuke.command(name="unban")
    @is_trusted()
    async def an_unban(self, ctx: commands.Context, state: str = None):
        """Prevent mass unbans"""
        await self._set_module(ctx, "unban", state)

    @antinuke.command(name="prune")
    @is_trusted()
    async def an_prune(self, ctx: commands.Context, state: str = None):
        """Prevent member prunes"""
        await self._set_module(ctx, "prune", state)

    @antinuke.command(name="integration")
    @is_trusted()
    async def an_integration(self, ctx: commands.Context, state: str = None):
        """Prevent unauthorized integrations"""
        await self._set_module(ctx, "integration", state)

    @antinuke.command(name="all")
    @is_trusted()
    async def an_all(self, ctx: commands.Context, state: str = None):
        """Turn every module on or off at once"""
        new = parse_state(state)
        if new is None:
            return await ctx.send(f"Usage: `{ctx.prefix}antinuke all <on/off>`")
        cfg = self.cfg(ctx.guild)
        for key in MODULES:
            cfg["modules"][key] = new
        self.store.save()
        await ctx.send(f"All {len(MODULES)} Anti-Nuke modules are now **{'on' if new else 'off'}**.")

    # ---- information (owner or admin)

    @antinuke.command(name="status")
    @is_trusted()
    async def an_status(self, ctx: commands.Context):
        """Quick overview of protection state"""
        cfg = self.cfg(ctx.guild)
        on = [k for k in MODULES if cfg["modules"][k]]
        off = [k for k in MODULES if not cfg["modules"][k]]
        embed = discord.Embed(title="Anti-Nuke status", color=COLOR)
        embed.add_field(name=f"Active ({len(on)}/{len(MODULES)})", value=", ".join(f"`{k}`" for k in on) or "None", inline=False)
        if off:
            embed.add_field(name="Disabled", value=", ".join(f"`{k}`" for k in off), inline=False)
        embed.add_field(name="Punishment", value=cfg["punishment"], inline=True)
        embed.add_field(name="Window", value=f"{cfg['window']}s", inline=True)
        embed.add_field(
            name="Log channel",
            value=f"<#{cfg['logchannel']}>" if cfg["logchannel"] else "Not set",
            inline=True,
        )
        embed.add_field(name="Violations on record", value=str(len(cfg["violations"])), inline=True)
        await ctx.send(embed=embed)

    @antinuke.command(name="violations", aliases=["history"])
    @is_trusted()
    async def an_violations(self, ctx: commands.Context, count: commands.Range[int, 1, 25] = 10):
        """Recent Anti-Nuke violations (default 10, max 25)"""
        history = self.cfg(ctx.guild)["violations"]
        if not history:
            return await ctx.send("No Anti-Nuke violations on record.")
        lines = [
            f"<t:{v['time']}:R> <@{v['user']}> (`{v['user']}`) - {v['mode']}\n> {v['reason']}"
            for v in reversed(history[-count:])
        ]
        embed = discord.Embed(
            title="Recent Anti-Nuke violations", description="\n".join(lines)[:4000], color=COLOR
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @antinuke.command(name="check", aliases=["userinfo"])
    @is_trusted()
    async def an_check(self, ctx: commands.Context, user: discord.User):
        """See a user's Anti-Nuke standing"""
        cfg = self.cfg(ctx.guild)
        guild = ctx.guild
        now = time.monotonic()
        window = cfg["window"]
        lines = [
            f"Server owner: {'yes' if user.id == guild.owner_id else 'no'}",
            f"Anti-Nuke admin: {'yes' if user.id in cfg['admins'] else 'no'}",
            f"Whitelisted: {'yes' if user.id in cfg['whitelist'] else 'no'}",
            f"Exempt from detection: {'yes' if self.exempt(guild, user.id, cfg) else 'no'}",
        ]
        counts = []
        for module in LIMIT_ACTIONS:
            hits = self.hits.get((guild.id, user.id, module))
            n = sum(1 for t in hits if now - t <= window) if hits else 0
            if n:
                counts.append(f"`{module}`: {n}/{cfg['limits'][module]}")
        lines.append("Recent actions: " + (", ".join(counts) if counts else "none in the current window"))
        past = sum(1 for v in cfg["violations"] if v["user"] == user.id)
        lines.append(f"Past violations on record: {past}")
        embed = discord.Embed(title=f"Anti-Nuke check: {user}", description="\n".join(lines), color=COLOR)
        await ctx.send(embed=embed)

    @antinuke.command(name="test")
    @is_trusted()
    async def an_test(self, ctx: commands.Context):
        """Send a test message to the log channel"""
        cfg = self.cfg(ctx.guild)
        channel = ctx.guild.get_channel(cfg["logchannel"]) if cfg["logchannel"] else None
        if channel is None:
            return await ctx.send(f"No log channel set. Use `{ctx.prefix}antinuke logchannel` first.")
        embed = discord.Embed(
            title="Anti-Nuke test",
            description=f"Test log sent by {ctx.author.mention}. Violation logs will appear here.",
            color=COLOR,
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            return await ctx.send(f"Couldn't send to {channel.mention}: {e}")
        await ctx.send(f"Test message sent to {channel.mention}.")

    @antinuke.command(name="botwls", aliases=["botwhitelistlist"])
    @is_trusted()
    async def an_botwls(self, ctx: commands.Context):
        """List whitelisted bots"""
        cfg = self.cfg(ctx.guild)
        embed = discord.Embed(
            title="Whitelisted bots", description=mention_list(cfg["botwhitelist"]), color=COLOR
        )
        await ctx.send(embed=embed)

    @antinuke.command(name="config")
    @is_trusted()
    async def an_config(self, ctx: commands.Context):
        """View server configuration"""
        cfg = self.cfg(ctx.guild)
        modules = "\n".join(
            f"{'✅' if cfg['modules'][k] else '❌'} `{k}`" for k in MODULES
        )
        log = f"<#{cfg['logchannel']}>" if cfg["logchannel"] else "Not set"
        embed = discord.Embed(title="Anti-Nuke configuration", color=COLOR)
        embed.add_field(name="Modules", value=modules, inline=False)
        embed.add_field(name="Punishment", value=cfg["punishment"], inline=True)
        embed.add_field(name="Window", value=f"{cfg['window']}s", inline=True)
        embed.add_field(name="Log channel", value=log, inline=True)
        embed.add_field(name="Admins", value=str(len(cfg["admins"])), inline=True)
        embed.add_field(name="Whitelisted", value=str(len(cfg["whitelist"])), inline=True)
        embed.add_field(
            name="Watched grants",
            value=", ".join(cfg["perm_grant"]) or "None",
            inline=False,
        )
        embed.add_field(
            name="Watched removals",
            value=", ".join(cfg["perm_remove"]) or "None",
            inline=False,
        )
        await ctx.send(embed=embed)

    async def _send_admins(self, ctx: commands.Context):
        cfg = self.cfg(ctx.guild)
        embed = discord.Embed(
            title="Anti-Nuke admins", description=mention_list(cfg["admins"]), color=COLOR
        )
        await ctx.send(embed=embed)

    @antinuke.command(name="admins")
    @is_trusted()
    async def an_admins(self, ctx: commands.Context):
        """View Anti-Nuke Admins"""
        await self._send_admins(ctx)

    @antinuke.command(name="list")
    @is_trusted()
    async def an_list(self, ctx: commands.Context):
        """View modules and whitelisted users"""
        cfg = self.cfg(ctx.guild)
        modules = "\n".join(
            f"{'✅' if cfg['modules'][k] else '❌'} `{k}` - {desc}" for k, desc in MODULES.items()
        )
        embed = discord.Embed(title="Anti-Nuke modules", description=modules, color=COLOR)
        embed.add_field(name="Whitelisted users", value=mention_list(cfg["whitelist"]), inline=False)
        await ctx.send(embed=embed)

    @antinuke.command(name="limits")
    @is_trusted()
    async def an_limits(self, ctx: commands.Context):
        """View action limits"""
        cfg = self.cfg(ctx.guild)
        lines = "\n".join(f"`{a}` - more than {cfg['limits'][a]}" for a in LIMIT_ACTIONS)
        embed = discord.Embed(
            title="Anti-Nuke limits",
            description=f"Punished when exceeded within **{cfg['window']}s**.\n\n{lines}",
            color=COLOR,
        )
        await ctx.send(embed=embed)

    @antinuke.command(name="health")
    @is_trusted()
    async def an_health(self, ctx: commands.Context):
        """Check permissions and hierarchy"""
        guild = ctx.guild
        cfg = self.cfg(guild)
        me = guild.me
        perms = me.guild_permissions

        needed = {
            "view_audit_log": "View Audit Log",
            "ban_members": "Ban Members",
            "kick_members": "Kick Members",
            "manage_roles": "Manage Roles",
            "manage_channels": "Manage Channels",
            "manage_webhooks": "Manage Webhooks",
        }
        lines = [
            f"{'✅' if getattr(perms, attr) else '❌'} {label}" for attr, label in needed.items()
        ]

        above = [r for r in guild.roles if r > me.top_role]
        if above:
            lines.append(
                f"⚠️ {len(above)} role(s) sit above the bot's top role, so members with them can't be punished."
            )
        else:
            lines.append("✅ The bot's top role is the highest in the server.")

        channel_id = cfg["logchannel"]
        if channel_id and guild.get_channel(channel_id):
            lines.append(f"✅ Log channel set (<#{channel_id}>)")
        else:
            lines.append("⚠️ No log channel set (use `logchannel`).")

        embed = discord.Embed(
            title="Anti-Nuke health", description="\n".join(lines), color=COLOR
        )
        await ctx.send(embed=embed)

    # ---- whitelist (owner or admin)

    async def _toggle_whitelist(self, ctx: commands.Context, user: discord.User):
        cfg = self.cfg(ctx.guild)
        if user.id in cfg["whitelist"]:
            cfg["whitelist"].remove(user.id)
            msg = f"Removed {user.mention} from the whitelist."
        else:
            cfg["whitelist"].append(user.id)
            msg = f"Added {user.mention} to the whitelist."
        self.store.save()
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    async def _send_whitelist(self, ctx: commands.Context):
        cfg = self.cfg(ctx.guild)
        embed = discord.Embed(
            title="Whitelisted users", description=mention_list(cfg["whitelist"]), color=COLOR
        )
        await ctx.send(embed=embed)

    @antinuke.command(name="botwl", aliases=["botwhitelist"])
    @is_trusted()
    async def an_botwl(self, ctx: commands.Context, bot_id: int):
        """Allow a specific bot to be added by anyone (run again to remove)"""
        cfg = self.cfg(ctx.guild)
        if bot_id in cfg["botwhitelist"]:
            cfg["botwhitelist"].remove(bot_id)
            msg = f"Bot `{bot_id}` removed from the bot whitelist."
        else:
            cfg["botwhitelist"].append(bot_id)
            msg = f"Bot `{bot_id}` can now be added to this server."
        self.store.save()
        await ctx.send(msg)

    @antinuke.group(name="whitelist", aliases=["wl"], invoke_without_command=True)
    @is_trusted()
    async def an_whitelist(self, ctx: commands.Context, user: discord.User = None):
        """Toggle whitelist status"""
        if user is None:
            return await ctx.send(f"Usage: `{ctx.prefix}antinuke whitelist @user`")
        await self._toggle_whitelist(ctx, user)

    @an_whitelist.command(name="add")
    @is_trusted()
    async def an_whitelist_add(self, ctx: commands.Context, user: discord.User):
        """Add to whitelist"""
        cfg = self.cfg(ctx.guild)
        if user.id in cfg["whitelist"]:
            return await ctx.send(f"{user.mention} is already whitelisted.", allowed_mentions=discord.AllowedMentions.none())
        cfg["whitelist"].append(user.id)
        self.store.save()
        await ctx.send(f"Added {user.mention} to the whitelist.", allowed_mentions=discord.AllowedMentions.none())

    @an_whitelist.command(name="remove")
    @is_trusted()
    async def an_whitelist_remove(self, ctx: commands.Context, user: discord.User):
        """Remove from whitelist"""
        cfg = self.cfg(ctx.guild)
        if user.id not in cfg["whitelist"]:
            return await ctx.send(f"{user.mention} isn't whitelisted.", allowed_mentions=discord.AllowedMentions.none())
        cfg["whitelist"].remove(user.id)
        self.store.save()
        await ctx.send(f"Removed {user.mention} from the whitelist.", allowed_mentions=discord.AllowedMentions.none())

    @an_whitelist.command(name="clear")
    @is_trusted()
    async def an_whitelist_clear(self, ctx: commands.Context, confirm: str = None):
        """Empty the whitelist"""
        cfg = self.cfg(ctx.guild)
        if confirm is None or confirm.lower() != "confirm":
            return await ctx.send(
                f"This removes all {len(cfg['whitelist'])} whitelisted user(s). "
                f"Run `{ctx.prefix}antinuke whitelist clear confirm` to continue."
            )
        cfg["whitelist"].clear()
        self.store.save()
        await ctx.send("The whitelist was cleared.")

    @antinuke.command(name="whitelistlist", aliases=["wls"])
    @is_trusted()
    async def an_whitelistlist(self, ctx: commands.Context):
        """List whitelisted users"""
        await self._send_whitelist(ctx)

    # top-level shortcuts: -wl @user and -wls
    @commands.command(name="wl")
    @is_trusted()
    async def wl_shortcut(self, ctx: commands.Context, user: discord.User):
        """Toggle whitelist status"""
        await self._toggle_whitelist(ctx, user)

    @commands.command(name="wls")
    @is_trusted()
    async def wls_shortcut(self, ctx: commands.Context):
        """List whitelisted users"""
        await self._send_whitelist(ctx)

    # ---- admins (server owner)

    async def _add_admin(self, ctx: commands.Context, user: discord.User):
        cfg = self.cfg(ctx.guild)
        if user.id in cfg["admins"]:
            return await ctx.send(f"{user.mention} is already an Anti-Nuke admin.", allowed_mentions=discord.AllowedMentions.none())
        cfg["admins"].append(user.id)
        self.store.save()
        await ctx.send(f"{user.mention} is now an Anti-Nuke admin.", allowed_mentions=discord.AllowedMentions.none())

    @antinuke.group(name="admin", invoke_without_command=True)
    @is_owner()
    async def an_admin(self, ctx: commands.Context, user: discord.User = None):
        """Add an Anti-Nuke Admin"""
        if user is None:
            return await ctx.send(f"Usage: `{ctx.prefix}antinuke admin @user`")
        await self._add_admin(ctx, user)

    @an_admin.command(name="add")
    @is_owner()
    async def an_admin_add(self, ctx: commands.Context, user: discord.User):
        """Add an Anti-Nuke Admin"""
        await self._add_admin(ctx, user)

    @an_admin.command(name="remove")
    @is_owner()
    async def an_admin_remove(self, ctx: commands.Context, user: discord.User):
        """Remove an Anti-Nuke Admin"""
        cfg = self.cfg(ctx.guild)
        if user.id not in cfg["admins"]:
            return await ctx.send(f"{user.mention} isn't an Anti-Nuke admin.", allowed_mentions=discord.AllowedMentions.none())
        cfg["admins"].remove(user.id)
        self.store.save()
        await ctx.send(f"Removed {user.mention} as an Anti-Nuke admin.", allowed_mentions=discord.AllowedMentions.none())

    @an_admin.command(name="clear")
    @is_owner()
    async def an_admin_clear(self, ctx: commands.Context, confirm: str = None):
        """Remove every Anti-Nuke Admin"""
        cfg = self.cfg(ctx.guild)
        if confirm is None or confirm.lower() != "confirm":
            return await ctx.send(
                f"This removes all {len(cfg['admins'])} Anti-Nuke admin(s). "
                f"Run `{ctx.prefix}antinuke admin clear confirm` to continue."
            )
        cfg["admins"].clear()
        self.store.save()
        await ctx.send("All Anti-Nuke admins were removed.")

    @an_admin.command(name="list")
    @is_trusted()
    async def an_admin_list(self, ctx: commands.Context):
        """View Anti-Nuke Admins"""
        await self._send_admins(ctx)

    # ---- server owner settings

    @antinuke.command(name="punishment")
    @is_owner()
    async def an_punishment(self, ctx: commands.Context, mode: str = None):
        """Set punishment (ban, kick or strip)"""
        cfg = self.cfg(ctx.guild)
        if mode is None or mode.lower() not in PUNISHMENTS:
            return await ctx.send(
                f"Current punishment: **{cfg['punishment']}**. "
                f"Usage: `{ctx.prefix}antinuke punishment <ban/kick/strip>`"
            )
        cfg["punishment"] = mode.lower()
        self.store.save()
        await ctx.send(f"Punishment set to **{cfg['punishment']}**.")

    @antinuke.command(name="logchannel")
    @is_owner()
    async def an_logchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set violation logs (defaults to this channel)"""
        cfg = self.cfg(ctx.guild)
        channel = channel or ctx.channel
        cfg["logchannel"] = channel.id
        self.store.save()
        await ctx.send(f"Violations will be logged in {channel.mention}.")

    @antinuke.command(name="logclear", aliases=["logoff"])
    @is_owner()
    async def an_logclear(self, ctx: commands.Context):
        """Stop logging violations"""
        cfg = self.cfg(ctx.guild)
        if cfg["logchannel"] is None:
            return await ctx.send("No log channel is set.")
        cfg["logchannel"] = None
        self.store.save()
        await ctx.send("Violation logging is turned off.")

    @antinuke.command(name="window")
    @is_owner()
    async def an_window(self, ctx: commands.Context, seconds: commands.Range[int, 3, 60]):
        """Set detection window in seconds (3-60)"""
        cfg = self.cfg(ctx.guild)
        cfg["window"] = seconds
        self.store.save()
        await ctx.send(f"Detection window set to **{seconds}s**.")

    @antinuke.command(name="limit")
    @is_owner()
    async def an_limit(self, ctx: commands.Context, action: str, count: commands.Range[int, 1, 20]):
        """Change an action limit (1-20)"""
        action = action.lower()
        if action not in LIMIT_ACTIONS:
            return await ctx.send(f"Action must be one of: {', '.join(f'`{a}`' for a in LIMIT_ACTIONS)}")
        cfg = self.cfg(ctx.guild)
        cfg["limits"][action] = count
        self.store.save()
        await ctx.send(f"`{action}` limit set to **{count}**. More than that within the window is punished.")

    @antinuke.group(name="permissions", aliases=["perms"], invoke_without_command=True)
    @is_owner()
    async def an_permissions(self, ctx: commands.Context):
        """Watch permission grants and removals"""
        cfg = self.cfg(ctx.guild)
        await ctx.send(
            f"Watched grants: {', '.join(cfg['perm_grant']) or 'None'}\n"
            f"Watched removals: {', '.join(cfg['perm_remove']) or 'None'}\n"
            f"Usage: `{ctx.prefix}antinuke permissions grant|remove <permission>`"
        )

    async def _toggle_watch(self, ctx: commands.Context, key: str, permission: str, verb: str):
        name = permission.lower().replace(" ", "_")
        if name not in ALL_PERMISSIONS:
            return await ctx.send(f"`{permission}` isn't a Discord permission (example: `administrator`, `manage_roles`).")
        cfg = self.cfg(ctx.guild)
        if name in cfg[key]:
            cfg[key].remove(name)
            await ctx.send(f"No longer watching permission **{verb}** of `{name}`.")
        else:
            cfg[key].append(name)
            await ctx.send(f"Now watching permission **{verb}** of `{name}`. Run again to stop watching.")
        self.store.save()

    @an_permissions.command(name="grant")
    @is_owner()
    async def an_permissions_grant(self, ctx: commands.Context, *, permission: str):
        """Watch permission grants"""
        await self._toggle_watch(ctx, "perm_grant", permission, "grants")

    @an_permissions.command(name="remove")
    @is_owner()
    async def an_permissions_remove(self, ctx: commands.Context, *, permission: str):
        """Watch permission removals"""
        await self._toggle_watch(ctx, "perm_remove", permission, "removals")

    @an_permissions.command(name="list")
    @is_owner()
    async def an_permissions_list(self, ctx: commands.Context):
        """Show watched permissions"""
        cfg = self.cfg(ctx.guild)
        embed = discord.Embed(title="Watched permissions", color=COLOR)
        embed.add_field(name="Grants", value=", ".join(f"`{p}`" for p in cfg["perm_grant"]) or "None", inline=False)
        embed.add_field(name="Removals", value=", ".join(f"`{p}`" for p in cfg["perm_remove"]) or "None", inline=False)
        await ctx.send(embed=embed)

    @an_permissions.command(name="clear")
    @is_owner()
    async def an_permissions_clear(self, ctx: commands.Context):
        """Stop watching all permissions"""
        cfg = self.cfg(ctx.guild)
        cfg["perm_grant"].clear()
        cfg["perm_remove"].clear()
        self.store.save()
        await ctx.send("No longer watching any permission grants or removals.")

    @antinuke.command(name="reset")
    @is_owner()
    async def an_reset(self, ctx: commands.Context, confirm: str = None):
        """Reset Anti-Nuke settings"""
        if confirm is None or confirm.lower() != "confirm":
            return await ctx.send(
                f"This wipes every Anti-Nuke setting for this server. Run `{ctx.prefix}antinuke reset confirm` to continue."
            )
        self.store.reset(ctx.guild.id)
        await ctx.send("Anti-Nuke settings were reset to defaults.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
