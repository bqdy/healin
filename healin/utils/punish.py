"""Punishment DMs in the same style as bleed's: a red embed with the server icon, who did it, why,
and a dispute footer.

    async with notify(ctx, user, "ban", reason):
        await ctx.guild.ban(user)

The DM goes out BEFORE the action (after a ban or kick the bot usually shares no server with the
person any more, so Discord would refuse the DM). If the action then fails, the DM is deleted
again so nobody is told they were punished when they weren't.
"""
import re
from contextlib import asynccontextmanager

import discord

COLOR = 0xE74C3C

# kind -> (embed title, first field name)
KINDS = {
    "ban": ("Banned", "You have been banned in"),
    "tempban": ("Banned", "You have been banned in"),
    "hardban": ("Banned", "You have been banned in"),
    "softban": ("Softbanned", "You have been softbanned in"),
    "kick": ("Kicked", "You have been kicked from"),
    "timeout": ("Timed Out", "You have been timed out in"),
    "mute": ("Muted", "You have been muted in"),
    "imute": ("Image Muted", "You have been image muted in"),
    "rmute": ("Reaction Muted", "You have been reaction muted in"),
    "jail": ("Jailed", "You have been jailed in"),
    "warn": ("Warned", "You have been warned in"),
}

_UNITS = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week"}
_NO_REASON = {"", "no reason given", "no reason provided"}


def pretty_duration(label):
    """'10m' -> '10 minutes'. Anything that isn't number+unit is shown as written."""
    m = re.fullmatch(r"(\d+)([smhdw])", (label or "").strip().lower())
    if not m:
        return label
    n = int(m[1])
    return f"{n} {_UNITS[m[2]]}{'' if n == 1 else 's'}"


def build_embed(guild, moderator, kind, reason=None, duration=None):
    title, lead = KINDS[kind]
    if (reason or "").strip().lower() in _NO_REASON:
        reason = "No reason provided"
    embed = discord.Embed(title=title, color=COLOR, timestamp=discord.utils.utcnow())
    embed.add_field(name=lead, value=guild.name[:1024], inline=True)
    embed.add_field(name="Moderator", value=moderator.name[:1024], inline=True)
    embed.add_field(name="Reason", value=reason.strip()[:1024], inline=True)
    if duration:
        embed.add_field(name="Duration", value=pretty_duration(duration), inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="If you would like to dispute this punishment, contact a staff member.")
    return embed


async def dm_punishment(guild, moderator, user, kind, reason=None, duration=None):
    """Send the punishment DM. Returns the message, or None if they have DMs closed / are a bot."""
    if getattr(user, "bot", False):
        return None
    try:
        return await user.send(embed=build_embed(guild, moderator, kind, reason, duration))
    except (discord.Forbidden, discord.HTTPException):
        return None


@asynccontextmanager
async def notify(ctx, user, kind, reason=None, duration=None):
    dm = await dm_punishment(ctx.guild, ctx.author, user, kind, reason, duration)
    try:
        yield dm
    except BaseException:
        if dm:
            try:
                await dm.delete()
            except discord.HTTPException:
                pass
        raise
