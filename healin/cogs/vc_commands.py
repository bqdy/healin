"""VC Commands cog: owner controls for voice channels, interface panels and voice stats.

Replaces cogs/vc_commands.py. Data lives in data/vc_commands.db (SQLite, standard library).
Pillow is optional (only used for the `stats list` image card).

The `-vc <subcommand>` commands are attached to the existing `vc` group (owned by the VC Utility
cog) once every cog has loaded. Any command name another cog already owns is skipped, not fatal.
"""

import asyncio
import io
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

try:
    from PIL import Image, ImageDraw, ImageFont

    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

DB_PATH = "data/vc_commands.db"
COLOR = 0x0F0F10
NO_PINGS = discord.AllowedMentions.none()

# Commands registered under `-vc` (everything else is top level).
SUBCOMMANDS = {
    "lock", "unlock", "ghost", "unghost", "claim", "transfer", "partner", "disconnect",
    "reject", "permit", "ban", "unban", "knock", "rename", "bitrate", "region", "limit",
    "count", "activity", "info",
}


class VCError(Exception):
    """A problem worth telling the user about in plain words."""


# --- helpers start (no discord objects needed) ---

def fmt_secs(total) -> str:
    total = int(total)
    d, r = divmod(total, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_leaderboard(title: str, entries) -> bytes:
    """entries: list of (rank, name, value) -> PNG bytes."""
    width, row, head = 900, 40, 90
    height = head + max(len(entries), 1) * row + 20
    img = Image.new("RGB", (width, height), (15, 15, 16))
    d = ImageDraw.Draw(img)
    big, mid = _font(40), _font(26)
    d.text((40, 24), title, fill=(255, 255, 255), font=big)
    podium = [(255, 215, 0), (192, 192, 192), (205, 127, 50)]
    for i, (rank, name, value) in enumerate(entries):
        y = head + i * row
        if i % 2 == 0:
            d.rectangle((20, y, width - 20, y + row - 4), fill=(24, 24, 27))
        color = podium[i] if i < 3 else (180, 180, 185)
        d.text((40, y + 6), f"#{rank}", fill=color, font=mid)
        d.text((140, y + 6), str(name)[:30], fill=(240, 240, 240), font=mid)
        w = d.textlength(value, font=mid)
        d.text((width - 40 - w, y + 6), value, fill=(240, 240, 240), font=mid)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

# --- helpers end ---


SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS partners (channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (channel_id, user_id));
CREATE TABLE IF NOT EXISTS permits (channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (channel_id, user_id));
CREATE TABLE IF NOT EXISTS bans (guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (guild_id, owner_id, user_id));
CREATE TABLE IF NOT EXISTS voice_stats (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    seconds INTEGER NOT NULL DEFAULT 0, sessions INTEGER NOT NULL DEFAULT 0,
    longest INTEGER NOT NULL DEFAULT 0, last_seen REAL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS config (guild_id INTEGER PRIMARY KEY, interface_channel INTEGER);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def run(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def close(self):
        self.conn.close()


# --------------------------------------------------------------- interface buttons

# act, emoji, name, what it does, button row
PANEL = [
    ("lock", "\U0001f512", "Lock", "the voice channel", 0),
    ("unlock", "\U0001f513", "Unlock", "the voice channel", 0),
    ("ghost", "\U0001f47b", "Ghost", "the voice channel", 0),
    ("reveal", "\U0001f441\ufe0f", "Reveal", "the voice channel", 0),
    ("claim", "\U0001f451", "Claim", "the voice channel", 0),
    ("rename", "\u270f\ufe0f", "Rename", "the voice channel", 1),
    ("limit", "\U0001f465", "Limit", "the user limit", 1),
    ("info", "\u2139\ufe0f", "View", "channel information", 1),
]
EMOJI = {act: emoji for act, emoji, _name, _desc, _row in PANEL}


class VCButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vcx:(?P<act>[a-z]+)"):
    """One icon button on the interface panel. Always acts on the voice channel the clicker is in."""

    def __init__(self, act: str, row: Optional[int] = None):
        emoji = EMOJI.get(act)
        super().__init__(
            discord.ui.Button(
                emoji=emoji,
                label=None if emoji else act.title(),
                style=discord.ButtonStyle.secondary,
                custom_id=f"vcx:{act}",
                row=row,
            )
        )
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["act"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("VC Commands")
        if cog is None:
            return await interaction.response.send_message("VC controls aren't available right now.", ephemeral=True)
        await cog.handle_button(interaction, self.act)


class RenameModal(discord.ui.Modal, title="Rename voice channel"):
    new_name = discord.ui.TextInput(label="New name", max_length=100)

    def __init__(self, cog: "VCCommands"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        text = await self.cog.safe(self.cog.op_rename, interaction.user, self.new_name.value)
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)


class LimitModal(discord.ui.Modal, title="Set user limit"):
    number = discord.ui.TextInput(label="Limit (0 = unlimited, max 99)", max_length=2)

    def __init__(self, cog: "VCCommands"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(str(self.number.value).strip())
        except ValueError:
            return await interaction.response.send_message("Enter a number from 0 to 99.", ephemeral=True)
        text = await self.cog.safe(self.cog.op_limit, interaction.user, value)
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)


def panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for act, _emoji, _name, _desc, row in PANEL:
        view.add_item(VCButton(act, row))
    return view


def panel_embed(member: Optional[discord.Member] = None, logo: Optional[str] = None) -> discord.Embed:
    """Welcome text plus a "Button Usage" list that puts each button's emoji next to what it does."""
    if member is not None:
        welcome = f"Welcome to your voice channel, {member.mention}\nUse the buttons below to control your voice channel."
    else:
        welcome = "Join a voice channel, then use the buttons below to control it."
    legend = "\n".join(f"> {emoji} - `{name}` {desc}" for _act, emoji, name, desc, _row in PANEL)
    embed = discord.Embed(description=f"{welcome}\n\n**Button Usage**\n{legend}", color=COLOR)
    embed.set_author(name="Voice Controls")
    if logo:
        embed.set_thumbnail(url=logo)
    return embed


class VCCommands(commands.Cog, name="VC Commands"):
    """Voice channel ownership, controls and stats"""

    help_text = """# VC Commands

**Channel Control:**
`{p}vc lock` - Lock your voice channel
`{p}vc unlock` - Unlock your voice channel
`{p}vc ghost` - Hide your voice channel
`{p}vc unghost` / `{p}vc reveal` - Reveal your voice channel
`{p}vc claim` - Claim when the current owner is outside the call
`{p}vc transfer @user` - Transfer ownership
`{p}vc partner @user` - Make a member co-owner
`{p}interface` - Post the channel controls in the VC chat
`{p}setinterface #channel` - Set the reusable interface channel

**User Management:**
`{p}vc disconnect @user` - Disconnect a member
`{p}vc reject @user` - Block a member from rejoining
`{p}vc permit @user` - Permit a rejected or banned member, or let them bypass autokick in this call
`{p}autokick [on/off]` / `{p}vc autokick [on/off]` - Channel owner only: disconnect anyone who joins your VC without a permit
`{p}vc ban @user` - Ban a member from your VC
`{p}vc unban @user` - Unban a member from your VC
`{p}vc knock` - Ping the current VC owner

**Channel Settings:**
`{p}vc rename <name>` - Rename the channel
`{p}vc bitrate <kbps>` - Set the bitrate
`{p}vc region <region>` - Set the voice region
`{p}vc limit <number>` - Set the user limit
`{p}vc count` - View the server voice count
`{p}mvc` - View live voice totals and state counts
`{p}vc activity` - View member voice activity states
`{p}vc info` - View channel information
`{p}stats [@user]` - View a member's voice profile
`{p}stats list` / `{p}status list` - View the top 20 voice members as an image card"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Store(DB_PATH)
        self.skipped = set()  # command names another cog already owns (filled in by setup)
        self._attached = []  # names of commands attached to the `vc` group
        self._start = {}  # (guild, user) -> when their current voice session began
        self._flushed = {}  # (guild, user) -> time up to which seconds are already saved

    async def cog_load(self):
        self.bot.add_dynamic_items(VCButton)
        self.flush_loop.start()

    def cog_unload(self):
        self.flush_loop.cancel()
        vc = self.bot.get_command("vc")
        if isinstance(vc, commands.Group):
            for name in self._attached:
                vc.remove_command(name)
        now = time.time()
        for key in list(self._start):
            self._close(key, now)
        self.db.close()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure) and not isinstance(
            error, (commands.MissingPermissions, commands.NoPrivateMessage)
        ):
            return
        if isinstance(error, commands.UserInputError):
            usage = f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            await ctx.send(f"{error}\nUsage: `{usage}`")
        elif isinstance(error, commands.MissingPermissions):
            needed = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            await ctx.send(f"You need **{needed}** to use that.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("That command only works in servers.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        else:
            import traceback

            traceback.print_exception(type(error), error, error.__traceback__)
            await ctx.send("Something went wrong running that command.")

    # ------------------------------------------------------------ ownership model

    def vm_entry(self, channel):
        """VoiceMaster's own record for a channel it created (None for any other channel)."""
        data = getattr(sys.modules.get("cogs.voicemaster"), "db", None)
        if not data:
            return None
        return data.get(str(channel.guild.id), {}).get("temp", {}).get(str(channel.id))

    def owner_id(self, channel) -> Optional[int]:
        entry = self.vm_entry(channel)
        if entry:
            return entry.get("owner")
        row = self.db.one("SELECT owner_id FROM owners WHERE channel_id = ?", (channel.id,))
        if row:
            return row["owner_id"]
        util = self.bot.get_cog("VC Utility")  # `vc forceclaim` / `forceownership` store owners there
        if util is not None:
            try:
                return util.cfg(channel.guild)["owners"].get(str(channel.id))
            except Exception:
                return None
        return None

    def set_owner(self, channel, user_id: int):
        self.db.run(
            "INSERT INTO owners (channel_id, guild_id, owner_id) VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET owner_id = excluded.owner_id",
            (channel.id, channel.guild.id, user_id),
        )
        entry = self.vm_entry(channel)  # keep VoiceMaster's record in step so both agree on the owner
        if entry is not None:
            entry["owner"] = user_id
            try:
                sys.modules["cogs.voicemaster"].save()
            except Exception:
                pass
        util = self.bot.get_cog("VC Utility")  # keep its owner list in step
        if util is not None:
            try:
                util.cfg(channel.guild)["owners"][str(channel.id)] = user_id
                util.store.save()
            except Exception:
                pass

    def partners(self, channel) -> set:
        rows = self.db.all("SELECT user_id FROM partners WHERE channel_id = ?", (channel.id,))
        return {r["user_id"] for r in rows}

    def is_permitted(self, channel_id: int, user_id: int) -> bool:
        """Used by VC Utility's autokick: permitted members are never autokicked from this call."""
        return self.db.one("SELECT 1 FROM permits WHERE channel_id = ? AND user_id = ?", (channel_id, user_id)) is not None

    def has_staff_perms(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.administrator or perms.manage_channels

    def controls(self, member: discord.Member, channel) -> bool:
        return (
            member.id == self.owner_id(channel)
            or member.id in self.partners(channel)
            or self.has_staff_perms(member)
        )

    def voice_of(self, member: discord.Member):
        vs = member.voice
        if vs is None or vs.channel is None:
            raise VCError("Join a voice channel first.")
        return vs.channel

    def need_control(self, member: discord.Member):
        channel = self.voice_of(member)
        if self.controls(member, channel):
            return channel
        if self.owner_id(channel) is None:
            raise VCError("This channel has no owner yet. Use `vc claim` to take it.")
        raise VCError("Only the channel owner or a co-owner can do that.")

    def need_owner(self, member: discord.Member):
        channel = self.voice_of(member)
        if member.id == self.owner_id(channel) or self.has_staff_perms(member):
            return channel
        if self.owner_id(channel) is None:
            raise VCError("This channel has no owner yet. Use `vc claim` to take it.")
        raise VCError("Only the channel owner can do that.")

    async def safe(self, fn, *args) -> str:
        try:
            return await fn(*args)
        except VCError as e:
            return str(e)
        except discord.Forbidden:
            return (
                "I'm missing a permission for that (Manage Channels, Manage Roles or Move Members), "
                "or that member/channel is above my role."
            )
        except discord.HTTPException as e:
            return f"Discord rejected that: {e.text or e}"

    async def _run(self, ctx: commands.Context, fn, *args):
        await ctx.send(await self.safe(fn, *args), allowed_mentions=NO_PINGS)

    async def set_default(self, channel, **perms):
        overwrite = channel.overwrites_for(channel.guild.default_role)
        for key, value in perms.items():
            setattr(overwrite, key, value)
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason="VC controls")

    async def keep_access(self, channel):
        """Make sure the owner and co-owners can still join/see after a lock or hide."""
        ids = set(self.partners(channel))
        owner = self.owner_id(channel)
        if owner:
            ids.add(owner)
        for uid in list(ids)[:10]:
            member = channel.guild.get_member(uid)
            if member is None:
                continue
            overwrite = channel.overwrites_for(member)
            if overwrite.connect is True and overwrite.view_channel is True:
                continue
            overwrite.connect = True
            overwrite.view_channel = True
            await channel.set_permissions(member, overwrite=overwrite, reason="VC owner access")

    # ----------------------------------------------------------------- operations

    async def op_lock(self, member):
        channel = self.need_control(member)
        await self.keep_access(channel)
        await self.set_default(channel, connect=False)
        return f"\U0001f512 Locked **{channel.name}**."

    async def op_unlock(self, member):
        channel = self.need_control(member)
        await self.set_default(channel, connect=True)
        return f"\U0001f513 Unlocked **{channel.name}**."

    async def op_ghost(self, member):
        channel = self.need_control(member)
        await self.keep_access(channel)
        await self.set_default(channel, view_channel=False)
        return f"\U0001f47b Hid **{channel.name}**."

    async def op_reveal(self, member):
        channel = self.need_control(member)
        await self.set_default(channel, view_channel=None)
        return f"\U0001f440 **{channel.name}** is visible again."

    async def rename_to_owner(self, channel, member):
        """Name the channel after its new owner. A failed rename (rate limit, perms) never blocks the claim."""
        # same name VoiceMaster gives a new channel (default "{user}'s VC"), e.g. "mycliq's VC"
        import sys

        template = "{user}'s VC"
        try:
            template = sys.modules["cogs.voicemaster"].conf(channel.guild.id).get("default_name") or template
        except Exception:
            pass
        name = (template.replace("{user}", member.display_name)
                .replace("{username}", member.name)
                .replace("{display}", member.display_name))
        name = " ".join(name.split())[:100]
        if not name or channel.name == name:
            return
        try:
            await channel.edit(name=name, reason=f"New owner: {member}")
        except discord.HTTPException:
            pass

    async def op_claim(self, member):
        channel = self.voice_of(member)
        owner = self.owner_id(channel)
        if owner == member.id:
            raise VCError("You already own this channel.")
        if owner is not None:
            owner_member = channel.guild.get_member(owner)
            if owner_member is not None and owner_member in channel.members:
                raise VCError(f"**{owner_member.display_name}** is still in the channel.")
        self.set_owner(channel, member.id)
        self.db.run("DELETE FROM partners WHERE channel_id = ? AND user_id = ?", (channel.id, member.id))
        await self.keep_access(channel)
        await self.rename_to_owner(channel, member)
        return f"\U0001f451 You now own **{channel.name}**."

    async def op_transfer(self, member, target):
        channel = self.need_owner(member)
        if target.bot or target.id == member.id:
            raise VCError("Pick another member.")
        if target not in channel.members:
            raise VCError(f"**{target.display_name}** isn't in your voice channel.")
        self.set_owner(channel, target.id)
        self.db.run("DELETE FROM partners WHERE channel_id = ? AND user_id = ?", (channel.id, target.id))
        await self.keep_access(channel)
        await self.rename_to_owner(channel, target)
        return f"\U0001f451 **{target.display_name}** now owns **{channel.name}**."

    async def op_partner(self, member, target):
        channel = self.need_owner(member)
        if target.bot or target.id == self.owner_id(channel):
            raise VCError("Pick another member.")
        if target.id in self.partners(channel):
            self.db.run("DELETE FROM partners WHERE channel_id = ? AND user_id = ?", (channel.id, target.id))
            return f"**{target.display_name}** is no longer a co-owner."
        self.db.run("INSERT OR IGNORE INTO partners (channel_id, user_id) VALUES (?, ?)", (channel.id, target.id))
        await self.keep_access(channel)
        return f"**{target.display_name}** is now a co-owner of **{channel.name}**."

    async def op_disconnect(self, member, target):
        channel = self.need_control(member)
        if target not in channel.members:
            raise VCError(f"**{target.display_name}** isn't in your voice channel.")
        if self.controls(target, channel):
            raise VCError("You can't disconnect the owner or a co-owner.")
        await target.move_to(None, reason=f"Disconnected by {member}")
        return f"Disconnected **{target.display_name}**."

    async def _block(self, channel, target):
        overwrite = channel.overwrites_for(target)
        overwrite.connect = False
        await channel.set_permissions(target, overwrite=overwrite, reason="VC reject/ban")
        self.db.run("DELETE FROM permits WHERE channel_id = ? AND user_id = ?", (channel.id, target.id))
        if target in channel.members:
            await target.move_to(None, reason="VC reject/ban")

    async def op_reject(self, member, target):
        channel = self.need_control(member)
        if target.bot or self.controls(target, channel):
            raise VCError("You can't reject that member.")
        await self._block(channel, target)
        return f"\U0001f6ab **{target.display_name}** can't rejoin **{channel.name}**."

    async def op_permit(self, member, target):
        channel = self.need_control(member)
        overwrite = channel.overwrites_for(target)
        overwrite.connect = True
        overwrite.view_channel = True
        await channel.set_permissions(target, overwrite=overwrite, reason=f"VC permit by {member}")
        owner = self.owner_id(channel)
        if owner:
            self.db.run("DELETE FROM bans WHERE guild_id = ? AND owner_id = ? AND user_id = ?", (channel.guild.id, owner, target.id))
        self.db.run("INSERT OR IGNORE INTO permits (channel_id, user_id) VALUES (?, ?)", (channel.id, target.id))
        note = ""
        limit = getattr(channel, "user_limit", 0)
        if limit and limit < 99 and target not in channel.members and len(channel.members) >= limit:
            await channel.edit(user_limit=limit + 1, reason=f"VC permit by {member}")
            note = f" The channel was full, so I raised the limit to {limit + 1}."
        return f"\u2705 **{target.display_name}** is permitted in **{channel.name}**.{note}"

    async def op_ban(self, member, target):
        channel = self.need_control(member)
        owner = self.owner_id(channel)
        if owner is None:
            raise VCError("This channel has no owner yet. Use `vc claim` first.")
        if target.bot or self.controls(target, channel):
            raise VCError("You can't ban that member.")
        self.db.run("INSERT OR IGNORE INTO bans (guild_id, owner_id, user_id) VALUES (?, ?, ?)", (channel.guild.id, owner, target.id))
        await self._block(channel, target)
        return f"\U0001f528 **{target.display_name}** is banned from your voice channels."

    async def op_unban(self, member, target):
        channel = self.need_control(member)
        owner = self.owner_id(channel)
        if owner is None:
            raise VCError("This channel has no owner yet.")
        cur = self.db.run("DELETE FROM bans WHERE guild_id = ? AND owner_id = ? AND user_id = ?", (channel.guild.id, owner, target.id))
        overwrite = channel.overwrites_for(target)
        if overwrite.connect is False:
            overwrite.connect = None
            await channel.set_permissions(target, overwrite=overwrite, reason=f"VC unban by {member}")
        if not cur.rowcount:
            return f"**{target.display_name}** wasn't banned."
        return f"\u2705 Unbanned **{target.display_name}**."

    async def op_rename(self, member, name: str):
        channel = self.need_control(member)
        name = " ".join(name.split())[:100]
        if not name:
            raise VCError("Give the channel a name.")
        await channel.edit(name=name, reason=f"VC rename by {member}")
        return f"Renamed the channel to **{name}**."

    async def op_bitrate(self, member, kbps: int):
        channel = self.need_control(member)
        limit = int(channel.guild.bitrate_limit)
        if not 8000 <= kbps * 1000 <= limit:
            raise VCError(f"Pick a bitrate between 8 and {limit // 1000} kbps.")
        await channel.edit(bitrate=kbps * 1000, reason=f"VC bitrate by {member}")
        return f"Bitrate set to **{kbps} kbps**."

    async def op_region(self, member, region: str):
        channel = self.need_control(member)
        region = region.lower().strip()
        rtc = None if region in ("auto", "automatic", "none") else region
        try:
            await channel.edit(rtc_region=rtc, reason=f"VC region by {member}")
        except discord.Forbidden:
            raise
        except discord.HTTPException:
            raise VCError(
                "Discord didn't accept that region. Try `auto`, `us-east`, `us-west`, `us-central`, "
                "`us-south`, `brazil`, `rotterdam`, `singapore`, `japan`, `sydney`, `india`, "
                "`hongkong`, `southafrica` or `russia`."
            )
        return f"Voice region set to **{rtc or 'automatic'}**."

    async def op_limit(self, member, number: int):
        channel = self.need_control(member)
        if not 0 <= number <= 99:
            raise VCError("The limit must be between 0 (unlimited) and 99.")
        await channel.edit(user_limit=number, reason=f"VC limit by {member}")
        return "Removed the user limit." if number == 0 else f"User limit set to **{number}**."

    def info_embed(self, channel) -> discord.Embed:
        owner = self.owner_id(channel)
        partners = self.partners(channel)
        default = channel.overwrites_for(channel.guild.default_role)
        embed = discord.Embed(title=channel.name, color=COLOR)
        embed.add_field(name="Owner", value=f"<@{owner}>" if owner else "*Unclaimed*")
        embed.add_field(name="Co-owners", value=", ".join(f"<@{p}>" for p in partners) or "*None*")
        limit = getattr(channel, "user_limit", 0) or "\u221e"
        embed.add_field(name="Members", value=f"{len(channel.members)}/{limit}")
        embed.add_field(name="Bitrate", value=f"{channel.bitrate // 1000} kbps")
        embed.add_field(name="Region", value=str(channel.rtc_region or "automatic"))
        embed.add_field(
            name="Status",
            value=f"{'Locked' if default.connect is False else 'Unlocked'} \u2022 {'Hidden' if default.view_channel is False else 'Visible'}",
        )
        embed.add_field(name="Created", value=f"<t:{int(channel.created_at.timestamp())}:R>")
        if channel.category:
            embed.add_field(name="Category", value=channel.category.name)
        return embed

    # ------------------------------------------------------------------ buttons

    async def handle_button(self, interaction: discord.Interaction, act: str):
        member = interaction.user
        if not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if act == "rename":
            return await interaction.response.send_modal(RenameModal(self))
        if act == "limit":
            return await interaction.response.send_modal(LimitModal(self))
        if act == "info":
            try:
                embed = self.info_embed(self.voice_of(member))
            except VCError as e:
                return await interaction.response.send_message(str(e), ephemeral=True)
            return await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)
        ops = {
            "lock": self.op_lock,
            "unlock": self.op_unlock,
            "ghost": self.op_ghost,
            "reveal": self.op_reveal,
            "claim": self.op_claim,
        }
        op = ops.get(act)
        if op is None:
            return await interaction.response.send_message("Unknown button.", ephemeral=True)
        await interaction.response.send_message(await self.safe(op, member), ephemeral=True, allowed_mentions=NO_PINGS)

    # ---------------------------------------------------------------- voice stats

    def _add_seconds(self, gid: int, uid: int, secs: float, session_len: Optional[float] = None):
        self.db.run(
            "INSERT INTO voice_stats (guild_id, user_id, seconds, sessions, longest, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET seconds = seconds + excluded.seconds, "
            "sessions = sessions + excluded.sessions, longest = MAX(longest, excluded.longest), "
            "last_seen = excluded.last_seen",
            (gid, uid, max(0, int(secs)), 1 if session_len is not None else 0, int(session_len or 0), time.time()),
        )

    def _close(self, key, now: float):
        start = self._start.pop(key, None)
        flushed = self._flushed.pop(key, None)
        if start is None or flushed is None:
            return
        self._add_seconds(key[0], key[1], now - flushed, session_len=now - start)

    def init_sessions(self):
        now = time.time()
        for guild in self.bot.guilds:
            for channel in guild.voice_channels + guild.stage_channels:
                for member in channel.members:
                    if not member.bot:
                        self._start.setdefault((guild.id, member.id), now)
                        self._flushed.setdefault((guild.id, member.id), now)

    def live_seconds(self, gid: int, uid: int) -> int:
        row = self.db.one("SELECT seconds FROM voice_stats WHERE guild_id = ? AND user_id = ?", (gid, uid))
        total = row["seconds"] if row else 0
        flushed = self._flushed.get((gid, uid))
        if flushed is not None:
            total += int(time.time() - flushed)
        return total

    @tasks.loop(minutes=5)
    async def flush_loop(self):
        now = time.time()
        for key in list(self._start):
            flushed = self._flushed.get(key)
            if flushed is not None:
                self._add_seconds(key[0], key[1], now - flushed)
                self._flushed[key] = now

    @flush_loop.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_ready(self):
        self.init_sessions()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        key = (member.guild.id, member.id)
        now = time.time()
        if "stats" not in self.skipped:
            if before.channel is None and after.channel is not None:
                self._start[key] = now
                self._flushed[key] = now
            elif before.channel is not None and after.channel is None:
                self._close(key, now)
        if "ban" not in self.skipped and after.channel is not None and before.channel != after.channel:
            owner = self.owner_id(after.channel)
            if owner and owner != member.id and self.db.one(
                "SELECT 1 FROM bans WHERE guild_id = ? AND owner_id = ? AND user_id = ?",
                (member.guild.id, owner, member.id),
            ):
                try:
                    await member.move_to(None, reason="VC ban")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        for table in ("owners", "partners", "permits"):
            self.db.run(f"DELETE FROM {table} WHERE channel_id = ?", (channel.id,))

    # ===================================================================== commands
    # The ones named in SUBCOMMANDS get attached to the `vc` group by setup().

    @commands.command(name="lock")
    async def vc_lock(self, ctx):
        """Lock your voice channel"""
        await self._run(ctx, self.op_lock, ctx.author)

    @commands.command(name="unlock")
    async def vc_unlock(self, ctx):
        """Unlock your voice channel"""
        await self._run(ctx, self.op_unlock, ctx.author)

    @commands.command(name="ghost")
    async def vc_ghost(self, ctx):
        """Hide your voice channel"""
        await self._run(ctx, self.op_ghost, ctx.author)

    @commands.command(name="unghost", aliases=["reveal"])
    async def vc_unghost(self, ctx):
        """Reveal your voice channel"""
        await self._run(ctx, self.op_reveal, ctx.author)

    @commands.command(name="claim")
    async def vc_claim(self, ctx):
        """Claim the channel when the owner has left"""
        await self._run(ctx, self.op_claim, ctx.author)

    @commands.command(name="transfer")
    async def vc_transfer(self, ctx, member: discord.Member):
        """Transfer ownership"""
        await self._run(ctx, self.op_transfer, ctx.author, member)

    @commands.command(name="partner")
    async def vc_partner(self, ctx, member: discord.Member):
        """Make a member co-owner (run again to remove)"""
        await self._run(ctx, self.op_partner, ctx.author, member)

    @commands.command(name="disconnect", aliases=["kick"])
    async def vc_disconnect(self, ctx, member: discord.Member):
        """Disconnect a member"""
        await self._run(ctx, self.op_disconnect, ctx.author, member)

    @commands.command(name="reject")
    async def vc_reject(self, ctx, member: discord.Member):
        """Block a member from rejoining"""
        await self._run(ctx, self.op_reject, ctx.author, member)

    @commands.command(name="permit")
    async def vc_permit(self, ctx, member: discord.Member):
        """Permit a rejected or banned member / bypass autokick in this call"""
        await self._run(ctx, self.op_permit, ctx.author, member)

    @commands.command(name="ban")
    async def vc_ban(self, ctx, member: discord.Member):
        """Ban a member from your VC"""
        await self._run(ctx, self.op_ban, ctx.author, member)

    @commands.command(name="unban")
    async def vc_unban(self, ctx, member: discord.Member):
        """Unban a member from your VC"""
        await self._run(ctx, self.op_unban, ctx.author, member)

    @commands.command(name="knock")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def vc_knock(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        """Ping the current VC owner"""
        try:
            channel = channel or self.voice_of(ctx.author)
        except VCError:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("Join a voice channel or name one: `vc knock #channel`.")
        owner = channel.guild.get_member(self.owner_id(channel) or 0)
        if owner is None:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f"**{channel.name}** has no owner to knock.")
        await ctx.send(
            f"\U0001f6aa {owner.mention}, **{ctx.author.display_name}** is knocking on **{channel.name}**.",
            allowed_mentions=discord.AllowedMentions(users=[owner]),
        )

    @commands.command(name="rename")
    async def vc_rename(self, ctx, *, name: str):
        """Rename the channel"""
        await self._run(ctx, self.op_rename, ctx.author, name)

    @commands.command(name="bitrate")
    async def vc_bitrate(self, ctx, kbps: int):
        """Set the bitrate"""
        await self._run(ctx, self.op_bitrate, ctx.author, kbps)

    @commands.command(name="region")
    async def vc_region(self, ctx, region: str):
        """Set the voice region"""
        await self._run(ctx, self.op_region, ctx.author, region)

    @commands.command(name="limit")
    async def vc_limit(self, ctx, number: int):
        """Set the user limit"""
        await self._run(ctx, self.op_limit, ctx.author, number)

    @commands.command(name="count")
    async def vc_count(self, ctx):
        """View the server voice count"""
        guild = ctx.guild
        channels = [c for c in guild.voice_channels + guild.stage_channels if c.members]
        total = sum(len(c.members) for c in channels)
        await ctx.send(f"\U0001f399\ufe0f **{total}** member(s) in voice across **{len(channels)}** channel(s).")

    @commands.command(name="activity")
    async def vc_activity(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        """View member voice activity states"""
        try:
            channel = channel or self.voice_of(ctx.author)
        except VCError as e:
            return await ctx.send(str(e))
        owner = self.owner_id(channel)
        lines = []
        for m in channel.members:
            vs = m.voice
            states = []
            if vs.mute or vs.self_mute:
                states.append("\U0001f507 muted")
            if vs.deaf or vs.self_deaf:
                states.append("\U0001f649 deafened")
            if vs.self_stream:
                states.append("\U0001f4fa streaming")
            if vs.self_video:
                states.append("\U0001f4f7 camera")
            crown = "\U0001f451 " if m.id == owner else ""
            lines.append(f"{crown}**{m.display_name}** \u2014 {', '.join(states) or '\U0001f399\ufe0f live'}")
        embed = discord.Embed(title=f"Activity in {channel.name}", description="\n".join(lines) or "Nobody is here.", color=COLOR)
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @commands.command(name="info")
    async def vc_info(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        """View channel information"""
        try:
            channel = channel or self.voice_of(ctx.author)
        except VCError as e:
            return await ctx.send(str(e))
        await ctx.send(embed=self.info_embed(channel), allowed_mentions=NO_PINGS)

    # top level

    @commands.command(name="interface")
    async def interface(self, ctx):
        """Post the channel controls in the VC chat"""
        try:
            channel = self.need_control(ctx.author)
        except VCError as e:
            return await ctx.send(str(e))
        try:
            await channel.send(embed=panel_embed(ctx.author, self.bot.user.display_avatar.url), view=panel_view())
        except discord.HTTPException:
            return await ctx.send("I couldn't post in that voice channel's chat (I need Send Messages there).")
        try:
            await ctx.message.add_reaction("\u2705")
        except discord.HTTPException:
            pass

    @commands.command(name="setinterface")
    @commands.has_permissions(manage_guild=True)
    async def setinterface(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Set the reusable interface channel"""
        if channel is None:
            row = self.db.one("SELECT interface_channel FROM config WHERE guild_id = ?", (ctx.guild.id,))
            current = ctx.guild.get_channel(row["interface_channel"]) if row and row["interface_channel"] else None
            return await ctx.send(f"Interface channel: {current.mention if current else '*not set*'}")
        try:
            await channel.send(embed=panel_embed(None, self.bot.user.display_avatar.url), view=panel_view())
        except discord.HTTPException:
            return await ctx.send(f"I couldn't post in {channel.mention} (I need Send Messages and Embed Links).")
        self.db.run(
            "INSERT INTO config (guild_id, interface_channel) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET interface_channel = excluded.interface_channel",
            (ctx.guild.id, channel.id),
        )
        await ctx.send(f"\u2705 Interface posted in {channel.mention}. Its buttons control whichever voice channel the clicker is in.")

    @commands.command(name="mvc")
    async def mvc(self, ctx):
        """View live voice totals and state counts"""
        guild = ctx.guild
        channels = [c for c in guild.voice_channels + guild.stage_channels if c.members]
        members = [m for c in channels for m in c.members]
        states = {
            "Muted": sum(1 for m in members if m.voice.mute or m.voice.self_mute),
            "Deafened": sum(1 for m in members if m.voice.deaf or m.voice.self_deaf),
            "Streaming": sum(1 for m in members if m.voice.self_stream),
            "Camera on": sum(1 for m in members if m.voice.self_video),
        }
        embed = discord.Embed(title="Live voice", color=COLOR)
        embed.add_field(name="In voice", value=str(len(members)))
        embed.add_field(name="Active channels", value=str(len(channels)))
        for name, value in states.items():
            embed.add_field(name=name, value=str(value))
        busiest = sorted(channels, key=lambda c: len(c.members), reverse=True)[:8]
        if busiest:
            embed.add_field(
                name="Busiest channels",
                value="\n".join(f"{c.mention} \u2014 {len(c.members)}" for c in busiest),
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.group(name="stats", aliases=["status"], invoke_without_command=True)
    async def stats(self, ctx, *, member: Optional[discord.Member] = None):
        """View a member's voice profile"""
        member = member or ctx.author
        row = self.db.one("SELECT * FROM voice_stats WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
        total = self.live_seconds(ctx.guild.id, member.id)
        if not row and total == 0:
            return await ctx.send(f"**{member.display_name}** has no recorded voice time yet.", allowed_mentions=NO_PINGS)
        sessions = row["sessions"] if row else 0
        longest = row["longest"] if row else 0
        rank = 1 + self.db.one(
            "SELECT COUNT(*) AS n FROM voice_stats WHERE guild_id = ? AND seconds > ?", (ctx.guild.id, total)
        )["n"]
        embed = discord.Embed(title=f"{member.display_name}'s voice profile", color=COLOR)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Total time", value=fmt_secs(total))
        embed.add_field(name="Rank", value=f"#{rank}")
        embed.add_field(name="Sessions", value=str(sessions))
        embed.add_field(name="Longest session", value=fmt_secs(longest))
        embed.add_field(name="Average session", value=fmt_secs(total / sessions) if sessions else "\u2014")
        if member.voice and member.voice.channel:
            embed.add_field(name="Right now", value=f"In {member.voice.channel.mention}", inline=False)
        elif row and row["last_seen"]:
            embed.add_field(name="Last seen", value=f"<t:{int(row['last_seen'])}:R>", inline=False)
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @stats.command(name="list")
    async def stats_list(self, ctx):
        """View the top 20 voice members"""
        rows = self.db.all("SELECT user_id FROM voice_stats WHERE guild_id = ?", (ctx.guild.id,))
        ids = {r["user_id"] for r in rows} | {uid for gid, uid in self._start if gid == ctx.guild.id}
        board = sorted(((self.live_seconds(ctx.guild.id, uid), uid) for uid in ids), reverse=True)[:20]
        board = [(secs, uid) for secs, uid in board if secs > 0]
        if not board:
            return await ctx.send("No voice time recorded yet.")
        entries = []
        for i, (secs, uid) in enumerate(board, start=1):
            member = ctx.guild.get_member(uid)
            entries.append((i, member.display_name if member else f"User {uid}", fmt_secs(secs)))
        title = f"Top voice members \u2022 {ctx.guild.name}"
        if HAVE_PIL:
            png = await asyncio.to_thread(draw_leaderboard, title, entries)
            return await ctx.send(file=discord.File(io.BytesIO(png), filename="voice_top.png"))
        lines = [f"`#{r}` {n} \u2014 {v}" for r, n, v in entries]
        await ctx.send(embed=discord.Embed(title=title, description="\n".join(lines), color=COLOR))


async def setup(bot: commands.Bot):
    """Register once every cog has loaded.

    Cogs load alphabetically, so the `vc` group (VC Utility) doesn't exist yet when this file
    loads. Waiting until the bot is ready lets us attach the `-vc ...` subcommands to it and skip
    any command name another cog already owns instead of failing.
    """
    cog = VCCommands(bot)

    def root(cmd):
        while cmd.parent is not None:
            cmd = cmd.parent
        return cmd

    async def register():
        try:
            await bot.wait_until_ready()
            vc = bot.get_command("vc")
            if not isinstance(vc, commands.Group):

                @commands.group(name="vc", invoke_without_command=True)
                async def vc_root(ctx):
                    """Voice utilities"""
                    await ctx.send(f"Use `{ctx.clean_prefix}help` to see the voice commands.")

                bot.add_command(vc_root)
                vc = vc_root

            # after a reload, the previous copy's subcommands are still attached to `vc`: drop them
            # so the fresh ones replace them instead of being skipped as "already taken"
            for old in list(vc.commands):
                if getattr(old.cog, "qualified_name", None) == "VC Commands":
                    vc.remove_command(old.name)

            taken_top, taken_sub = set(bot.all_commands), set(vc.all_commands)
            skipped, subs = [], []
            for cmd in [c for c in cog.__cog_commands__ if c.parent is None]:
                if cmd.name in SUBCOMMANDS:
                    if cmd.name in taken_sub:
                        skipped.append(cmd)
                    else:
                        cmd.aliases = [a for a in cmd.aliases if a not in taken_sub]
                        subs.append(cmd)
                elif cmd.name in taken_top:
                    skipped.append(cmd)
                else:
                    cmd.aliases = [a for a in cmd.aliases if a not in taken_top]

            removed = {id(c) for c in skipped} | {id(c) for c in subs}
            cog.__cog_commands__ = tuple(c for c in cog.__cog_commands__ if id(root(c)) not in removed)
            cog.skipped = {c.name for c in skipped}

            await bot.add_cog(cog)
            for cmd in subs:
                cmd.cog = cog
                vc.add_command(cmd)
                cog._attached.append(cmd.name)
            cog.init_sessions()

            print(f"[vc_commands] attached {len(subs)} command(s) to `vc`")
            if cog.skipped:
                print("[vc_commands] skipped (name already used by another cog): " + ", ".join(sorted(cog.skipped)))
        except Exception as e:
            print(f"FAILED to register VC Commands: {e!r}")

    bot._vc_commands_task = asyncio.create_task(register())