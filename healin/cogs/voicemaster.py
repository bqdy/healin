"""
VoiceMaster cog for discord.py 2.4+   ->   save as  cogs/voicemaster.py
(replace your current cogs/voicemaster.py with this one)

Needs:   pip install -U discord.py
Intents: Server Members + Voice States (+ Message Content for prefix commands)
Data:    voicemaster_data.json (created next to the bot)

How it works
- Joining a hub (primary / secondary) creates a personal voice channel and moves you in.
  The channel is deleted when it empties.
- Hub 1 uses category slot 1, hub 2 uses slot 2 (falls back to slot 1, then the hub's own category).
- Random hub: joining it drops you into a random voice channel in the allowed categories.
- Auto-unmute channels: anyone who joins gets their server-mute removed.

Panel look
- A rounded card: "VoiceMaster", a welcome line with the owner's @mention, then a "Button Usage"
  list with each button's emoji right beside what it does, and your logo at the top right.
- Two rows of emoji-only buttons (no words) under the card:
    row 1: lock, unlock, ghost, reveal, claim
    row 2: manage (permit/reject), rename, info, + user limit, - user limit
- The card needs discord.py 2.6+ (older versions get the same text in a normal embed).
- Emoji: the bot's application emojis from the Developer Portal (set USE_CUSTOM_EMOJI = False
  to use standard emoji instead).
- Logo (top right of the panel), first match wins:
    1. VM_LOGO_URL in your .env (a direct image link)
    2. a file called voicemaster_logo.png next to the bot (next to main.py)
    3. the bot's own avatar
"""
import asyncio
import json
import os
import pathlib
import random
import time
import traceback
from typing import Optional

import discord
from discord.ext import commands

DATA_FILE = "voicemaster_data.json"
LOGO_FILE = "voicemaster_logo.png"
DEFAULT_NAME = "{user}'s VC"
MAX_CATEGORIES = 10
PANEL_COLOR = 0x2B2D31

# Custom emoji = this bot's APPLICATION emojis (Developer Portal > your app > Emojis).
# They work in every server without the bot having to share a server with them.
# The IDs below come from your Developer Portal; on startup the bot also looks the emojis up by
# name, so if you ever re-upload one, just keep the same name (Lock, UnLock, Hide, ...).
CUSTOM_EMOJI = {
    "lock": "<:Lock:1568803216311984240>",
    "unlock": "<:UnLock:1568803217637113886>",
    "ghost": "<:Hide:1568803218673246319>",
    "reveal": "<:Unhide:1568803219616833586>",
    "claim": "<:Claim:1568803220292247695>",
    "manage": "<:Settings:1568803226168463370>",
    "rename": "<:Edit:1568803224897585282>",
    "info": "<:Information:1568803221617508512>",
    "plus": "<:Add:1568803222825476116>",
    "minus": "<:Less:1568803223895146536>",
}
# panel button -> name of the emoji in the Developer Portal (not case sensitive)
APP_EMOJI_NAMES = {
    "lock": "lock", "unlock": "unlock", "ghost": "hide", "reveal": "unhide", "claim": "claim",
    "manage": "settings", "rename": "edit", "info": "information", "plus": "add", "minus": "less",
}
# Standard emoji fallback. Set USE_CUSTOM_EMOJI = False if the custom ones don't show up.
UNICODE_EMOJI = {
    "lock": "\U0001f512", "unlock": "\U0001f513", "ghost": "\U0001f47b",
    "reveal": "\U0001f441\ufe0f", "claim": "\U0001f451", "manage": "\U0001f39b\ufe0f",
    "rename": "\u270f\ufe0f", "info": "\u2139\ufe0f", "plus": "\u2795", "minus": "\u2796",
}
USE_CUSTOM_EMOJI = True
EMOJI = CUSTOM_EMOJI if USE_CUSTOM_EMOJI else UNICODE_EMOJI

# key, name shown in "Button Usage", what it does, button row
_ITEMS = [
    ("lock", "Lock", "the voice channel", 0),
    ("unlock", "Unlock", "the voice channel", 0),
    ("ghost", "Ghost", "the voice channel", 0),
    ("reveal", "Reveal", "the voice channel", 0),
    ("claim", "Claim", "the voice channel", 0),
    ("manage", "Manage", "permit or reject members", 1),
    ("rename", "Rename", "the voice channel", 1),
    ("info", "View", "channel information", 1),
    ("plus", "Increase", "the user limit", 1),
    ("minus", "Decrease", "the user limit", 1),
]
# key, emoji, name, what it does, row  (buttons show the emoji only)
PANEL_ITEMS = [(k, EMOJI[k], name, desc, row) for k, name, desc, row in _ITEMS]

db = {}
if os.path.exists(DATA_FILE):
    try:
        with open(DATA_FILE) as f:
            db = json.load(f)
    except (OSError, json.JSONDecodeError):
        print("[voicemaster] could not read voicemaster_data.json, starting fresh")


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, DATA_FILE)


def conf(guild_id) -> dict:
    c = db.setdefault(str(guild_id), {})
    c.setdefault("hub", None)
    c.setdefault("hub2", None)
    c.setdefault("randomhub", None)
    c.setdefault("interface", None)  # text channel that holds the control panel
    c.setdefault("randomcats", [])
    c.setdefault("categories", [])   # [{"id": int|None, "prefix": str, "sequential": bool}]
    c.setdefault("default_name", DEFAULT_NAME)
    c.setdefault("autounmute", [None, None])
    c.setdefault("temp", {})         # channel_id -> {"owner": int, "cat": int|None, "num": int|None}
    return c


async def send(ctx, content=None, **kwargs):
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    return await ctx.send(content, **kwargs)


def staff():
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if await ctx.bot.is_owner(ctx.author):
            return True
        if ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator:
            return True
        raise commands.MissingPermissions(["manage_guild"])
    return commands.check(predicate)


def owner_check():
    """Server owner or bot owner."""
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.author == ctx.guild.owner or await ctx.bot.is_owner(ctx.author):
            return True
        raise commands.CheckFailure("Only the server owner or bot owner can manage VoiceMaster categories.")
    return commands.check(predicate)


def parse_on(text: str) -> bool:
    t = text.lower()
    if t in ("on", "true", "enable", "yes"):
        return True
    if t in ("off", "false", "disable", "no"):
        return False
    raise commands.BadArgument("Use on or off.")


# --------------------------------------------------------------------------
# Panel text
# --------------------------------------------------------------------------
def panel_text(mention: Optional[str] = None, prefix: str = "-") -> str:
    """The text of the card: title, welcome line, and the emoji + button usage list."""
    lines = ["**VoiceMaster**", ""]
    lines.append("Welcome to your voice channel" + (f", {mention}" if mention else "."))
    lines.append("Use the buttons below to control your voice channel.")
    lines += ["", "### Button Usage"]
    for _key, emoji, name, desc, _row in PANEL_ITEMS:
        lines.append(f"> {emoji} - `{name}` {desc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Modals / pickers
# --------------------------------------------------------------------------
class NameModal(discord.ui.Modal, title="Rename channel"):
    value = discord.ui.TextInput(label="New name", max_length=100)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        name = self.value.value[:100]
        await self.channel.edit(name=name, reason="VoiceMaster rename")
        await interaction.response.send_message(f"✏️ Renamed to **{name}**.", ephemeral=True)


class ManageView(discord.ui.View):
    """Ephemeral picker: choose members, then Permit or Reject them."""

    def __init__(self, channel: discord.VoiceChannel, owner_id: int):
        super().__init__(timeout=120)
        self.channel, self.owner_id = channel, owner_id
        self.select = discord.ui.UserSelect(placeholder="Select members…", min_values=1, max_values=10)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the channel owner can use this.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

    async def apply(self, interaction: discord.Interaction, allow: bool):
        picked = [u for u in self.select.values if not u.bot and u.id != self.owner_id]
        if not picked:
            return await interaction.response.send_message("Pick at least one member first.", ephemeral=True)
        names = []
        for u in picked:
            member = interaction.guild.get_member(u.id)
            if member is None:
                continue
            if allow:
                await self.channel.set_permissions(member, connect=True, view_channel=True,
                                                   reason="VoiceMaster permit")
            else:
                await self.channel.set_permissions(member, connect=False, reason="VoiceMaster reject")
                if member.voice and member.voice.channel == self.channel:
                    try:
                        await member.move_to(None)
                    except discord.HTTPException:
                        pass
            names.append(member.display_name)
        verb = "✅ Permitted" if allow else "🚫 Rejected"
        await interaction.response.send_message(f"{verb}: {', '.join(names) or 'nobody'}", ephemeral=True)

    @discord.ui.button(label="Permit", style=discord.ButtonStyle.success)
    async def permit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply(interaction, True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply(interaction, False)


# --------------------------------------------------------------------------
# Button handling
# --------------------------------------------------------------------------
async def handle_button(interaction: discord.Interaction, key: str):
    guild = interaction.guild
    c = conf(guild.id)
    channel_id = interaction.channel_id
    entry = c["temp"].get(str(channel_id))
    if not entry:   # panel posted somewhere else: act on the voice channel the clicker is in
        vs = getattr(interaction.user, "voice", None)
        if vs and vs.channel and str(vs.channel.id) in c["temp"]:
            channel_id = vs.channel.id
            entry = c["temp"][str(channel_id)]
    ch = guild.get_channel(channel_id)

    async def reply(text):
        await interaction.response.send_message(text, ephemeral=True)

    if not entry or not isinstance(ch, discord.VoiceChannel):
        return await reply("This isn't an active VoiceMaster channel.")
    user = interaction.user

    if key == "info":
        ow = ch.overwrites_for(guild.default_role)
        e = discord.Embed(title=ch.name, color=PANEL_COLOR)
        e.add_field(name="Owner", value=f"<@{entry['owner']}>")
        e.add_field(name="Members", value=str(len(ch.members)))
        e.add_field(name="User limit", value=str(ch.user_limit or "No limit"))
        e.add_field(name="Locked", value="Yes" if ow.connect is False else "No")
        e.add_field(name="Hidden", value="Yes" if ow.view_channel is False else "No")
        e.add_field(name="Bitrate", value=f"{ch.bitrate // 1000} kbps")
        return await interaction.response.send_message(embed=e, ephemeral=True)

    if key == "claim":
        owner = guild.get_member(entry["owner"])
        if owner and owner.voice and owner.voice.channel == ch:
            return await reply("The owner is still in the channel.")
        if not (user.voice and user.voice.channel == ch):
            return await reply("Join the channel first.")
        if owner:
            await ch.set_permissions(owner, manage_channels=None, move_members=None, reason="VoiceMaster claim")
        await ch.set_permissions(user, view_channel=True, connect=True, manage_channels=True,
                                 move_members=True, reason="VoiceMaster claim")
        entry["owner"] = user.id
        save()
        return await reply("👑 You now own this channel.")

    if user.id != entry["owner"]:
        return await reply("Only the channel owner can use this.")

    everyone = guild.default_role
    if key == "lock":
        await ch.set_permissions(everyone, connect=False, reason="VoiceMaster lock")
        return await reply("🔒 Channel locked.")
    if key == "unlock":
        await ch.set_permissions(everyone, connect=True, reason="VoiceMaster unlock")
        return await reply("🔓 Channel unlocked.")
    if key == "ghost":
        await ch.set_permissions(everyone, view_channel=False, reason="VoiceMaster ghost")
        return await reply("👻 Channel hidden.")
    if key == "reveal":
        await ch.set_permissions(everyone, view_channel=None, reason="VoiceMaster reveal")
        return await reply("👁️ Channel visible again.")
    if key == "rename":
        return await interaction.response.send_modal(NameModal(ch))
    if key == "manage":
        return await interaction.response.send_message("Select members to permit or reject:",
                                                       view=ManageView(ch, entry["owner"]), ephemeral=True)
    if key == "limit":   # only on panels posted by older versions
        return await interaction.response.send_modal(LimitModal(ch))
    if key in ("plus", "minus"):
        cur = ch.user_limit or len(ch.members)
        new = min(cur + 1, 99) if key == "plus" else max(cur - 1, 0)
        await ch.edit(user_limit=new or None, reason="VoiceMaster limit")
        return await reply(f"👥 User limit: {new or 'no limit'}")


class LimitModal(discord.ui.Modal, title="Set user limit"):
    number = discord.ui.TextInput(label="Limit (0 = no limit, max 99)", max_length=2)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.number.value).strip())
        except ValueError:
            return await interaction.response.send_message("Enter a number from 0 to 99.", ephemeral=True)
        if not 0 <= n <= 99:
            return await interaction.response.send_message("The limit must be between 0 and 99.", ephemeral=True)
        await self.channel.edit(user_limit=n or None, reason="VoiceMaster limit")
        await interaction.response.send_message(f"\U0001f465 User limit: {n or 'no limit'}", ephemeral=True)


class VMButton(discord.ui.Button):
    """Grey emoji-only button; persists across restarts via its custom_id."""

    def __init__(self, key: str, emoji: Optional[str] = None, label: Optional[str] = None,
                 row: Optional[int] = None):
        super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label,
                         custom_id=f"vm:{key}", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await handle_button(interaction, self.key)


# --------------------------------------------------------------------------
# Panel: a rounded card (discord.py 2.6+) or the same text in an embed
# --------------------------------------------------------------------------
HAS_V2 = hasattr(discord.ui, "LayoutView")

if HAS_V2:
    class PanelLayout(discord.ui.LayoutView):
        """The card (text + logo on the right) with the emoji button rows under it."""

        def __init__(self, mention: Optional[str] = None, logo: Optional[str] = None, prefix: str = "-"):
            super().__init__(timeout=None)
            text = discord.ui.TextDisplay(panel_text(mention, prefix))
            body = discord.ui.Section(text, accessory=discord.ui.Thumbnail(logo)) if logo else text
            self.add_item(discord.ui.Container(body))
            for row in (0, 1):
                self.add_item(discord.ui.ActionRow(
                    *[VMButton(k, e) for k, e, _name, _desc, r in PANEL_ITEMS if r == row]))


class PanelClassic(discord.ui.View):
    """Fallback for discord.py < 2.6: same text in an embed, same emoji buttons."""

    def __init__(self):
        super().__init__(timeout=None)
        for k, e, _name, _desc, r in PANEL_ITEMS:
            self.add_item(VMButton(k, e, row=r))


def build_panel(mention: Optional[str], logo: Optional[str], prefix: str = "-", classic: bool = False):
    """Returns (view, embed_or_None). classic=True forces the embed version."""
    if HAS_V2 and not classic:
        return PanelLayout(mention, logo, prefix), None
    embed = discord.Embed(color=PANEL_COLOR, description=panel_text(mention, prefix))
    if logo:
        embed.set_thumbnail(url=logo)
    return PanelClassic(), embed


class VoiceMaster(commands.Cog):
    # Page shown in the main help menu ({p} becomes the server's prefix).
    help_text = (
        "# VoiceMaster\n\n"
        "**Required:** Manage Server (categories: Server Owner)\n\n"
        "## Setup\n"
        "`{p}vm` - Show the VoiceMaster overview and the current hubs\n"
        "`{p}vm setup` - Create a category, a \"Join to Create\" channel and an `interface` channel with the control panel\n"
        "`{p}vm hub set <channel>` - Use an existing voice channel as the hub\n"
        "`{p}vm hub create [name]` - Create a hub channel\n"
        "`{p}vm hub2 set <channel>` - Set a second hub\n"
        "`{p}vm hub2 create [name]` - Create a second hub\n"
        "`{p}vm hub2 remove` - Remove the second hub\n\n"
        "## Categories\n"
        "**Required:** Server Owner\n"
        "`{p}vm category add [name]` - Add a category slot\n"
        "`{p}vm category remove <slot>` - Remove a slot\n"
        "`{p}vm category set <slot> <category>` - Point a slot at a category\n"
        "`{p}vm category list` - View the slots\n"
        "`{p}vm category prefix <slot> <text>` - Name prefix for channels in a slot\n"
        "`{p}vm category sequential <slot> on/off` - Number channels in order\n\n"
        "## Channel Names\n"
        "`{p}vm default name [template]` - Default name for new channels (`{user}`, `{username}`)\n\n"
        "## Random Hub\n"
        "`{p}vm randomhub set <channel>` - Joining it drops you in a random voice channel\n"
        "`{p}vm randomhub category add <category>` - Allow a category\n"
        "`{p}vm randomhub category remove <category>` - Remove a category\n\n"
        "## Auto-Unmute\n"
        "`{p}vm autounmute set <channel>` - Remove the server mute from anyone who joins\n"
        "`{p}vm autounmute2 set <channel>` - A second auto-unmute channel\n\n"
        "## Panel\n"
        "`{p}vm panel` - Preview the control panel in this channel"
    )

    def __init__(self, bot):
        self.bot = bot
        self.last_create = {}

    # ---- lifecycle -------------------------------------------------------
    def logo(self):
        """(url_for_the_thumbnail, discord.File_or_None)"""
        url = os.getenv("VM_LOGO_URL")
        if url:
            return url, None
        for path in (pathlib.Path(LOGO_FILE), pathlib.Path(__file__).with_name(LOGO_FILE)):
            if path.exists():
                return f"attachment://{LOGO_FILE}", discord.File(path, filename=LOGO_FILE)
        return self.bot.user.display_avatar.url, None

    async def send_panel(self, channel, member=None):
        """Posts the interface (and pings the member, if given).
        If the card is rejected for any reason, the error is printed and the embed version is sent."""
        logo, logo_file = self.logo()
        prefix = self.bot.guild_prefix(getattr(channel, "guild", None))
        mention = member.mention if member else None
        ping = discord.AllowedMentions(users=[member]) if member else discord.AllowedMentions.none()
        try:
            view, embed = build_panel(mention, logo, prefix)
            kwargs = {"view": view, "allowed_mentions": ping}
            if embed is not None:
                kwargs["embed"] = embed
                if mention:
                    kwargs["content"] = mention
            if logo_file:
                kwargs["file"] = logo_file
            return await channel.send(**kwargs)
        except Exception as e:
            print(f"[voicemaster] couldn't send the card layout: {e!r}")
            traceback.print_exc()
        view, embed = build_panel(mention, self.bot.user.display_avatar.url, prefix, classic=True)
        return await channel.send(content=mention, embed=embed, view=view, allowed_mentions=ping)

    async def refresh_emoji(self):
        """Look the bot's application emojis up by name so the panel always uses the real ones."""
        if not USE_CUSTOM_EMOJI:
            return
        try:
            found = {e.name.lower(): e for e in await self.bot.fetch_application_emojis()}
        except Exception as e:
            print(f"[voicemaster] couldn't read the application emojis, using the saved IDs: {e!r}")
            return
        used, missing = 0, []
        for key, name in APP_EMOJI_NAMES.items():
            emoji = found.get(name)
            if emoji is None:
                missing.append(name)
                continue
            EMOJI[key] = str(emoji)
            used += 1
        PANEL_ITEMS[:] = [(k, EMOJI[k], name, desc, row) for k, name, desc, row in _ITEMS]
        print(f"[voicemaster] application emojis: {used}/{len(APP_EMOJI_NAMES)} found"
              + (f" (missing: {', '.join(missing)})" if missing else ""))

    async def cog_load(self):
        await self.refresh_emoji()
        print(f"[voicemaster] v6 loaded - discord.py {discord.__version__} - "
              f"{'card panel' if HAS_V2 else 'embed panel (needs discord.py 2.6+ for the card)'}")
        self.bot.add_view(PanelLayout() if HAS_V2 else PanelClassic())
        legacy = discord.ui.View(timeout=None)   # "limit" button on panels posted by earlier versions
        legacy.add_item(VMButton("limit", "\U0001f465"))
        self.bot.add_view(legacy)
        # Drop temp channels that emptied or vanished while the bot was offline.
        for gid, c in list(db.items()):
            guild = self.bot.get_guild(int(gid))
            for cid in list(c.get("temp", {})):
                ch = guild.get_channel(int(cid)) if guild else None
                if ch is None:
                    c["temp"].pop(cid, None)
                elif not ch.members:
                    try:
                        await ch.delete(reason="VoiceMaster cleanup")
                    except discord.HTTPException:
                        pass
                    c["temp"].pop(cid, None)
        save()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
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
            msg = str(error) or "You can't use that here."
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            msg = f"Something went wrong: `{error}`"
        await send(ctx, msg)

    # ======================================================================
    # VOICE LOGIC
    # ======================================================================
    def slot_category(self, guild, c, slot: int, hub: discord.VoiceChannel):
        """Return (category_channel_or_None, slot_index_or_None) for a hub."""
        cats = c["categories"]
        for idx in (slot - 1, 0):
            if 0 <= idx < len(cats) and cats[idx].get("id"):
                cat = guild.get_channel(cats[idx]["id"])
                if isinstance(cat, discord.CategoryChannel):
                    return cat, idx
        return hub.category, None

    def render_name(self, member, c, idx) -> tuple:
        template = c["default_name"] or DEFAULT_NAME
        name = (template.replace("{user}", member.display_name)
                .replace("{username}", member.name)
                .replace("{display}", member.display_name))
        prefix, num = "", None
        if idx is not None:
            slot = c["categories"][idx]
            prefix = slot.get("prefix") or ""
            if slot.get("sequential"):
                used = {t.get("num") for t in c["temp"].values()
                        if t.get("cat") == slot.get("id") and t.get("num")}
                num = 1
                while num in used:
                    num += 1
        parts = [prefix, f"{num}" if num else "", name]
        return " ".join(p for p in parts if p).strip()[:100] or "VC", num

    async def create_vc(self, member, c, slot: int, hub):
        now = time.time()
        if now - self.last_create.get(member.id, 0) < 3:   # anti join-spam
            return
        self.last_create[member.id] = now
        guild = member.guild
        cat, idx = self.slot_category(guild, c, slot, hub)
        name, num = self.render_name(member, c, idx)
        ow = dict(cat.overwrites) if cat else {}
        ow[member] = discord.PermissionOverwrite(view_channel=True, connect=True,
                                                 manage_channels=True, move_members=True)
        ch = None
        try:
            ch = await guild.create_voice_channel(name, category=cat, overwrites=ow,
                                                  reason=f"VoiceMaster: {member}")
            c["temp"][str(ch.id)] = {"owner": member.id, "cat": cat.id if cat else None, "num": num}
            save()
            await member.move_to(ch)
        except discord.HTTPException:
            # couldn't create or move them (they left?) -> tidy up
            if ch:
                c["temp"].pop(str(ch.id), None)
                save()
                try:
                    await ch.delete()
                except discord.HTTPException:
                    pass
            return
        try:
            await self.send_panel(ch, member)
        except Exception:
            print("[voicemaster] couldn't send the panel:")
            traceback.print_exc()

    async def random_join(self, member, c):
        guild = member.guild
        hubs = {c["hub"], c["hub2"], c["randomhub"]}
        pool = []
        for cid in c["randomcats"]:
            cat = guild.get_channel(cid)
            if isinstance(cat, discord.CategoryChannel):
                pool += [v for v in cat.voice_channels if v.id not in hubs
                         and v.permissions_for(member).connect]
        busy = [v for v in pool if v.members]
        target = random.choice(busy or pool) if pool else None
        try:
            await member.move_to(target)   # None disconnects if there is nowhere to go
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        c = conf(member.guild.id)
        if after.channel and after.channel != before.channel:
            cid = after.channel.id
            if cid == c["hub"]:
                await self.create_vc(member, c, 1, after.channel)
            elif cid == c["hub2"]:
                await self.create_vc(member, c, 2, after.channel)
            elif cid == c["randomhub"]:
                await self.random_join(member, c)
            if cid in [x for x in c["autounmute"] if x] and after.mute:
                try:
                    await member.edit(mute=False, reason="VoiceMaster auto-unmute")
                except discord.HTTPException:
                    pass
        if before.channel and before.channel != after.channel:
            if str(before.channel.id) in c["temp"] and not before.channel.members:
                c["temp"].pop(str(before.channel.id), None)
                save()
                try:
                    await before.channel.delete(reason="VoiceMaster: channel empty")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        c = db.get(str(channel.guild.id))
        if not c:
            return
        changed = c.get("temp", {}).pop(str(channel.id), None) is not None
        for key in ("hub", "hub2", "randomhub", "interface"):
            if c.get(key) == channel.id:
                c[key] = None
                changed = True
        if channel.id in c.get("autounmute", []):
            c["autounmute"] = [None if x == channel.id else x for x in c["autounmute"]]
            changed = True
        if changed:
            save()

    # ======================================================================
    # COMMANDS
    # ======================================================================
    @commands.group(name="vm", invoke_without_command=True)
    @staff()
    async def vm(self, ctx):
        p = ctx.clean_prefix + "vm"
        c = conf(ctx.guild.id)

        def ch(i):
            return f"<#{i}>" if i else "not set"

        await send(ctx, embed=discord.Embed(
            title="VoiceMaster",
            description=(
                f"**Setup:** `{p} setup` · `{p} hub set|create` · `{p} hub2 set|create|remove`\n"
                f"**Categories (owner):** `{p} category add|remove|set|list|prefix|sequential`\n"
                f"**Default name:** `{p} default name [template]` (`{{user}}`, `{{username}}`)\n"
                f"**Random hub:** `{p} randomhub set` · `{p} randomhub category add|remove`\n"
                f"**Auto-unmute:** `{p} autounmute set` · `{p} autounmute2 set`\n\n"
                f"Hub: {ch(c['hub'])} · Hub 2: {ch(c['hub2'])} · Random: {ch(c['randomhub'])}\n"
                f"Template: `{c['default_name']}`\n"
                f"Preview the interface: `{p} panel`")).set_footer(text="VoiceMaster v6 (card panel)"))

    # ---- setup / hubs -------------------------------------------------------
    @vm.command(name="setup")
    @staff()
    async def vm_setup(self, ctx):
        c = conf(ctx.guild.id)
        cat = None
        if c["categories"] and c["categories"][0].get("id"):
            cat = ctx.guild.get_channel(c["categories"][0]["id"])
        if not isinstance(cat, discord.CategoryChannel):
            cat = await ctx.guild.create_category("VoiceMaster", reason="VoiceMaster setup")
            if c["categories"]:
                c["categories"][0]["id"] = cat.id
            else:
                c["categories"].append({"id": cat.id, "prefix": "", "sequential": False})
        hub = await ctx.guild.create_voice_channel("Join to Create", category=cat,
                                                   reason="VoiceMaster setup")
        c["hub"] = hub.id

        # An "interface" text channel with the control panel, so members can see the buttons.
        # Re-running setup keeps the existing one instead of posting a second panel.
        interface = ctx.guild.get_channel(c["interface"]) if c["interface"] else None
        note = ""
        if not isinstance(interface, discord.TextChannel):
            interface = await self.create_interface(ctx.guild, cat)
            if interface is None:
                c["interface"] = None
                note = "\n⚠️ I couldn't create the interface channel (check my Manage Channels permission)."
            else:
                c["interface"] = interface.id
                try:
                    await self.send_panel(interface)
                except discord.HTTPException as e:
                    print(f"[voicemaster] couldn't post the panel in the interface channel: {e!r}")
                    note = f"\n⚠️ I made {interface.mention} but couldn't post the panel. Use `{ctx.clean_prefix}vm panel` there."
        save()
        extra = f" Controls are in {interface.mention}." if isinstance(interface, discord.TextChannel) else ""
        await send(ctx, f"✅ VoiceMaster ready. Join {hub.mention} to get your own channel "
                        f"(category: **{cat.name}**, slot 1).{extra}{note}")

    async def create_interface(self, guild, cat):
        """Read-only text channel for the panel: everyone can see it, only the bot can post."""
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                            add_reactions=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  attach_files=True),
        }
        for kwargs in ({"overwrites": overwrites}, {}):  # second try: plain channel, if overwrites are refused
            try:
                return await guild.create_text_channel("interface", category=cat,
                                                       reason="VoiceMaster setup", **kwargs)
            except discord.HTTPException as e:
                print(f"[voicemaster] couldn't create the interface channel ({'locked' if kwargs else 'plain'}): {e!r}")
        return None

    @vm.group(name="hub", invoke_without_command=True)
    @staff()
    async def vm_hub(self, ctx):
        await send(ctx, f"Usage: `{ctx.clean_prefix}vm hub set <channelID>` or "
                        f"`{ctx.clean_prefix}vm hub create [name]`")

    @vm_hub.command(name="set")
    @staff()
    async def vm_hub_set(self, ctx, channel: discord.VoiceChannel):
        conf(ctx.guild.id)["hub"] = channel.id
        save()
        await send(ctx, f"✅ Primary hub is now {channel.mention}.")

    @vm_hub.command(name="create")
    @staff()
    async def vm_hub_create(self, ctx, *, name: str = "Join to Create"):
        c = conf(ctx.guild.id)
        cat = None
        if c["categories"] and c["categories"][0].get("id"):
            cat = ctx.guild.get_channel(c["categories"][0]["id"])
        ch = await ctx.guild.create_voice_channel(name[:100], category=cat if isinstance(cat, discord.CategoryChannel) else None)
        c["hub"] = ch.id
        save()
        await send(ctx, f"✅ Created primary hub {ch.mention}.")

    @vm.group(name="hub2", invoke_without_command=True)
    @staff()
    async def vm_hub2(self, ctx):
        await send(ctx, f"Usage: `{ctx.clean_prefix}vm hub2 set <channelID>` · `create [name]` · `remove`")

    @vm_hub2.command(name="set")
    @staff()
    async def vm_hub2_set(self, ctx, channel: discord.VoiceChannel):
        conf(ctx.guild.id)["hub2"] = channel.id
        save()
        await send(ctx, f"✅ Secondary hub is now {channel.mention}.")

    @vm_hub2.command(name="create")
    @staff()
    async def vm_hub2_create(self, ctx, *, name: str = "Join to Create 2"):
        c = conf(ctx.guild.id)
        cats = c["categories"]
        cat = ctx.guild.get_channel(cats[1]["id"]) if len(cats) > 1 and cats[1].get("id") else None
        ch = await ctx.guild.create_voice_channel(name[:100], category=cat if isinstance(cat, discord.CategoryChannel) else None)
        c["hub2"] = ch.id
        save()
        await send(ctx, f"✅ Created secondary hub {ch.mention}.")

    @vm_hub2.command(name="remove")
    @staff()
    async def vm_hub2_remove(self, ctx):
        c = conf(ctx.guild.id)
        if not c["hub2"]:
            return await send(ctx, "No secondary hub is set.")
        c["hub2"] = None
        save()
        await send(ctx, "🗑️ Secondary hub removed (the channel itself was left in place).")

    # ---- categories (server owner / bot owner) -----------------------------
    @vm.group(name="category", invoke_without_command=True)
    @owner_check()
    async def vm_category(self, ctx):
        p = ctx.clean_prefix + "vm category"
        await send(ctx, f"`{p} add [name]` · `{p} remove <slot>` · `{p} set <slot> <categoryID>` · "
                        f"`{p} list` · `{p} prefix <slot> <text>` · `{p} sequential <slot> <on/off>`")

    def _slot(self, c, slot: int):
        if slot < 1 or slot > len(c["categories"]):
            raise commands.BadArgument("No such slot.")
        return c["categories"][slot - 1]

    @vm_category.command(name="add")
    @owner_check()
    async def vm_category_add(self, ctx, *, name: str = None):
        c = conf(ctx.guild.id)
        if len(c["categories"]) >= MAX_CATEGORIES:
            return await send(ctx, f"You can have at most {MAX_CATEGORIES} categories.")
        n = len(c["categories"]) + 1
        cat = await ctx.guild.create_category((name or f"VoiceMaster {n}")[:100], reason="VoiceMaster category")
        c["categories"].append({"id": cat.id, "prefix": "", "sequential": False})
        save()
        await send(ctx, f"✅ Added category **{cat.name}** as slot {n}.")

    @vm_category.command(name="remove")
    @owner_check()
    async def vm_category_remove(self, ctx, slot: int):
        c = conf(ctx.guild.id)
        removed = self._slot(c, slot)
        c["categories"].pop(slot - 1)
        save()
        await send(ctx, f"🗑️ Removed slot {slot} (<#{removed['id']}>). The Discord category was left in place.")

    @vm_category.command(name="set")
    @owner_check()
    async def vm_category_set(self, ctx, slot: int, category: discord.CategoryChannel):
        c = conf(ctx.guild.id)
        if slot == len(c["categories"]) + 1 and slot <= MAX_CATEGORIES:
            c["categories"].append({"id": category.id, "prefix": "", "sequential": False})
        else:
            self._slot(c, slot)["id"] = category.id
        save()
        await send(ctx, f"✅ Slot {slot} is now **{category.name}**.")

    @vm_category.command(name="list")
    @owner_check()
    async def vm_category_list(self, ctx):
        c = conf(ctx.guild.id)
        if not c["categories"]:
            return await send(ctx, "No categories configured. Use `vm setup` or `vm category add`.")
        lines = [f"`{i}.` <#{s['id']}> · prefix: `{s.get('prefix') or '-'}` · "
                 f"numbered: {'on' if s.get('sequential') else 'off'}"
                 for i, s in enumerate(c["categories"], 1)]
        await send(ctx, embed=discord.Embed(title="VoiceMaster categories", description="\n".join(lines)))

    @vm_category.command(name="prefix")
    @owner_check()
    async def vm_category_prefix(self, ctx, slot: int, *, text: str):
        c = conf(ctx.guild.id)
        self._slot(c, slot)["prefix"] = "" if text.lower() in ("none", "off", "clear") else text[:20]
        save()
        await send(ctx, f"✅ Slot {slot} prefix is now `{c['categories'][slot - 1]['prefix'] or '-'}`.")

    @vm_category.command(name="sequential")
    @owner_check()
    async def vm_category_sequential(self, ctx, slot: int, state: str):
        c = conf(ctx.guild.id)
        on = parse_on(state)
        self._slot(c, slot)["sequential"] = on
        save()
        await send(ctx, f"🔢 Numbered names for slot {slot}: **{'on' if on else 'off'}**.")

    # ---- default name -----------------------------------------------------
    @vm.group(name="default", invoke_without_command=True)
    @staff()
    async def vm_default(self, ctx):
        await send(ctx, f"Usage: `{ctx.clean_prefix}vm default name [template]`")

    @vm_default.command(name="name")
    @staff()
    async def vm_default_name(self, ctx, *, template: str = None):
        c = conf(ctx.guild.id)
        if not template:
            c["default_name"] = DEFAULT_NAME
            save()
            return await send(ctx, f"♻️ Template reset to `{DEFAULT_NAME}`.")
        c["default_name"] = template[:90]
        save()
        await send(ctx, f"✅ New voice channels will be named `{c['default_name']}` "
                        f"(`{{user}}` = display name, `{{username}}` = account name).")

    # ---- random hub -------------------------------------------------------
    @vm.group(name="randomhub", invoke_without_command=True)
    @staff()
    async def vm_randomhub(self, ctx):
        p = ctx.clean_prefix + "vm randomhub"
        await send(ctx, f"`{p} set <channelID>` · `{p} category add|remove <categoryID>`")

    @vm_randomhub.command(name="set")
    @staff()
    async def vm_randomhub_set(self, ctx, channel: discord.VoiceChannel):
        conf(ctx.guild.id)["randomhub"] = channel.id
        save()
        await send(ctx, f"🎲 Random hub is now {channel.mention}.")

    @vm_randomhub.group(name="category", invoke_without_command=True)
    @staff()
    async def vm_randomhub_category(self, ctx):
        c = conf(ctx.guild.id)
        await send(ctx, "**Allowed categories**\n" + ("\n".join(f"<#{i}>" for i in c["randomcats"]) or "None")
                   + f"\nUse `{ctx.clean_prefix}vm randomhub category add|remove <categoryID>`.")

    @vm_randomhub_category.command(name="add")
    @staff()
    async def vm_randomhub_category_add(self, ctx, category: discord.CategoryChannel):
        c = conf(ctx.guild.id)
        if category.id not in c["randomcats"]:
            c["randomcats"].append(category.id)
        save()
        await send(ctx, f"✅ **{category.name}** is now allowed for the random hub.")

    @vm_randomhub_category.command(name="remove")
    @staff()
    async def vm_randomhub_category_remove(self, ctx, category: discord.CategoryChannel):
        c = conf(ctx.guild.id)
        if category.id in c["randomcats"]:
            c["randomcats"].remove(category.id)
        save()
        await send(ctx, f"🗑️ **{category.name}** removed from the random hub.")

    # ---- auto-unmute ------------------------------------------------------
    @vm.group(name="autounmute", invoke_without_command=True)
    @staff()
    async def vm_autounmute(self, ctx):
        await send(ctx, f"Usage: `{ctx.clean_prefix}vm autounmute set <channelID>`")

    @vm_autounmute.command(name="set")
    @staff()
    async def vm_autounmute_set(self, ctx, channel: discord.VoiceChannel):
        conf(ctx.guild.id)["autounmute"][0] = channel.id
        save()
        await send(ctx, f"🔊 Auto-unmute channel 1 is now {channel.mention}.")

    @vm.group(name="autounmute2", invoke_without_command=True)
    @staff()
    async def vm_autounmute2(self, ctx):
        await send(ctx, f"Usage: `{ctx.clean_prefix}vm autounmute2 set <channelID>`")

    @vm_autounmute2.command(name="set")
    @staff()
    async def vm_autounmute2_set(self, ctx, channel: discord.VoiceChannel):
        conf(ctx.guild.id)["autounmute"][1] = channel.id
        save()
        await send(ctx, f"🔊 Auto-unmute channel 2 is now {channel.mention}.")

    @vm.command(name="panel")
    @staff()
    async def vm_panel(self, ctx):
        """Preview the panel in the current channel (handy for checking the look)."""
        await self.send_panel(ctx.channel, ctx.author)


async def setup(bot):
    """Registers after the bot is ready (every other cog has loaded), and skips any command
    name or alias another cog already owns instead of failing."""
    cog = VoiceMaster(bot)

    async def register():
        try:
            await bot.wait_until_ready()
            taken = bot.all_commands
            clash = [c.name for c in cog.get_commands() if c.name in taken]
            if clash:
                print(f"[voicemaster] skipped (name already used by another cog): {', '.join(clash)}")
                cog.__cog_commands__ = tuple(c for c in cog.__cog_commands__ if c.name not in taken)
            await bot.add_cog(cog)
            print("[voicemaster] commands registered")
        except Exception:
            traceback.print_exc()

    bot._voicemaster_register_task = asyncio.create_task(register())