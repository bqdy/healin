import os
import time
from collections import defaultdict

import discord
from discord.ext import commands, tasks

from utils.store import GuildStore

COLOR = 0x0F0F10
NO_PINGS = discord.AllowedMentions.none()

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "482506940027502597"))

# Same levels as cogs/rank_management.py
LEVEL_BOT_OWNER = 100
LEVEL_SERVER_OWNER = 90
LEVEL_FOOL = 70
LEVEL_GOD = 60
LEVEL_PRES = 50
LEVEL_CLOVER = 30
LEVEL_CHEIF = 20
LEVEL_MEMBER = 10

DEFAULTS = {
    "shield": [],  # shielded users (set by Chief+)
    "antikick": [],  # users who turned on their own anti-kick
    "antidrag": [],  # users who turned on their own anti-drag
    "godmode": [],  # users with Godmode (set by God+)
    "stfu": [],  # persistent voice mute
    "blacklist": [],  # disconnected whenever they join voice
    "autokick": [],  # old per-user list, no longer used (autokick is now per voice channel)
    "autokick_vcs": [],  # voice channel ids with autokick turned on by their owner
    "owners": {},  # voice channel id -> owner user id
}

HISTORY_DEFAULTS = {"events": []}  # [user id, from channel id, to channel id, unix time]
HISTORY_MAX = 300


class ChainRequest(discord.ui.View):
    """Accept / Decline buttons for `-vc chain`."""

    def __init__(self, cog: "VCUtility", inviter: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.cog = cog
        self.inviter = inviter
        self.target = target
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This request isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        moved = await self.cog.add_follow(self.inviter.guild, self.target, self.inviter)
        text = f"{self.target.display_name} joined {self.inviter.display_name}'s chain."
        if moved:
            text += " Moved to their call."
        await interaction.response.edit_message(content=text, view=None, allowed_mentions=NO_PINGS)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"{self.target.display_name} declined the chain request.",
            view=None,
            allowed_mentions=NO_PINGS,
        )
        self.stop()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="Chain request expired.", view=None, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass


class VCUtility(commands.Cog, name="VC Utility"):
    """Ranked voice utilities and protection"""

    help_text = """# VC Utility

**Everyone:**
`{p}vc follow [@user]` / `{p}follow [@user]` - Follow and move to their call
`{p}vc unfollow [@user/all]` / `{p}unfollow [@user/all]` - Stop following
`{p}vc chain @user @user ...` - Send chain requests

**Chief & Above:**
`{p}vc shield @user` / `{p}vc unshield @user` - Manage shields
`{p}vc antikick` / `{p}vc antidrag` / `{p}vc antikickdrag` - Toggle your protection
`{p}vc drag @user` - Move a member into your call
`{p}vc dragall #channel` - Move unprotected members
`{p}vc voicehistory [@user]` - View voice history

**Clover & Above:**
`{p}vc forceclaim` - Claim ownership
`{p}vc autokick [on/off]` / `{p}autokick [on/off]` - VC owner only: toggle autokick for your own voice channel

**Pres & Above:**
`{p}vc forceownership @user` - Assign ownership
`{p}vc stfu @user` / `{p}vc unstfu @user` - Manage persistent voice mute
`{p}vc stfu list` / `{p}vc stfu wipeall` - List or clear persistent mutes

**God & Above:**
`{p}godmode @user` / `{p}vc godmode @user` - Toggle protection
`{p}vc ungodmode @user` - Remove protection

**Fool & Above:**
`{p}godmode list` / `{p}godmode wipeall` - List or clear Godmode

**Administrator:**
`{p}moveall #channel` - Move everyone from your current VC

**Sunset Access:**
`{p}vc blacklist @user` / `{p}vc blacklist remove @user` / `{p}vc blacklist list`"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GuildStore("vc_utility", DEFAULTS)
        self.history = GuildStore("vc_history", HISTORY_DEFAULTS)
        self.history_dirty = False
        # (guild id, follower id) -> set of user ids they follow. Lives in memory only.
        self.follows = defaultdict(set)

    async def cog_load(self):
        self.flush_history.start()

    async def cog_unload(self):
        self.flush_history.cancel()
        if self.history_dirty:
            self.history.save()

    def cfg(self, guild: discord.Guild) -> dict:
        return self.store.get(guild.id)

    # ------------------------------------------------------------------ plumbing

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}"
            return await ctx.send(f"Usage: `{usage}`")
        if isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                return await ctx.send(
                    "I'm missing a permission for that (Move Members or Mute Members), or the "
                    "target is above my role."
                )
            if isinstance(original, discord.HTTPException):
                return await ctx.send(f"Discord rejected that: {original.text or original}")
        raise error

    # ------------------------------------------------------------------- helpers

    def level(self, guild: discord.Guild, user_id: int) -> int:
        ranks = self.bot.get_cog("Rank Management")
        if ranks is not None:
            return ranks.level(guild, user_id)
        if user_id == BOT_OWNER_ID:
            return LEVEL_BOT_OWNER
        if user_id == guild.owner_id:
            return LEVEL_SERVER_OWNER
        return LEVEL_MEMBER

    async def need(self, ctx: commands.Context, minimum: int, label: str) -> bool:
        if self.level(ctx.guild, ctx.author.id) >= minimum:
            return True
        await ctx.send(f"You need the **{label}** rank or above to use that.")
        return False

    def outranks(self, guild: discord.Guild, caller_id: int, target_id: int) -> bool:
        return caller_id == BOT_OWNER_ID or self.level(guild, caller_id) > self.level(guild, target_id)

    def protection(self, guild: discord.Guild, caller_id: int, target: discord.Member, kind: str):
        """Why `target` can't be dragged/kicked by `caller_id`, or None if they can."""
        if caller_id == BOT_OWNER_ID:
            return None
        cfg = self.cfg(guild)
        if target.id in cfg["godmode"]:
            return "has Godmode"
        if self.level(guild, caller_id) >= LEVEL_SERVER_OWNER:
            return None
        if target.id in cfg["shield"]:
            return "is shielded"
        if kind == "drag" and target.id in cfg["antidrag"]:
            return "has anti-drag on"
        if kind == "kick" and target.id in cfg["antikick"]:
            return "has anti-kick on"
        return None

    def toggle(self, guild: discord.Guild, key: str, user_id: int) -> bool:
        """Flip membership in one of the lists. Returns True if now on."""
        items = self.cfg(guild)[key]
        if user_id in items:
            items.remove(user_id)
            now = False
        else:
            items.append(user_id)
            now = True
        self.store.save()
        return now

    async def bot_can(self, ctx: commands.Context, permission: str, label: str) -> bool:
        if getattr(ctx.guild.me.guild_permissions, permission):
            return True
        await ctx.send(f"I need the **{label}** permission for that.")
        return False

    async def disconnect(self, member: discord.Member, reason: str) -> bool:
        try:
            await member.move_to(None, reason=reason)
            return True
        except discord.HTTPException:
            return False

    async def free_voice(self, member: discord.Member, state: discord.VoiceState | None = None):
        """Undo a server mute/deafen on a Godmode member.

        A persistent stfu mute is left alone so the two features don't fight each other.
        """
        state = state or member.voice
        if state is None or state.channel is None:
            return
        kwargs = {}
        if state.mute and member.id not in self.cfg(member.guild)["stfu"]:
            kwargs["mute"] = False
        if state.deaf:
            kwargs["deafen"] = False
        if not kwargs:
            return
        try:
            await member.edit(reason="Godmode", **kwargs)
        except discord.HTTPException:
            pass

    async def add_follow(self, guild: discord.Guild, follower: discord.Member, target: discord.Member) -> bool:
        """Start following `target`. Returns True if the follower was moved right away."""
        self.follows[(guild.id, follower.id)].add(target.id)
        if (
            follower.voice
            and follower.voice.channel
            and target.voice
            and target.voice.channel
            and follower.voice.channel != target.voice.channel
            and target.voice.channel.permissions_for(follower).connect
        ):
            try:
                await follower.move_to(target.voice.channel, reason=f"Following {target}")
                return True
            except discord.HTTPException:
                return False
        return False

    # ------------------------------------------------------------ voice history

    def record(self, guild: discord.Guild, user_id: int, before, after):
        events = self.history.get(guild.id)["events"]
        events.append([user_id, before.id if before else None, after.id if after else None, int(time.time())])
        del events[:-HISTORY_MAX]
        self.history_dirty = True

    @tasks.loop(seconds=30)
    async def flush_history(self):
        if self.history_dirty:
            self.history_dirty = False
            self.history.save()

    # ---------------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.bot:
            return
        guild = member.guild
        cfg = self.cfg(guild)
        changed = before.channel != after.channel

        if changed:
            self.record(guild, member.id, before.channel, after.channel)

        # blacklisted users get thrown out of voice
        if after.channel and member.id in cfg["blacklist"]:
            await self.disconnect(member, "VC blacklist")
            return

        # godmode: undo any server mute / deafen
        if after.channel and member.id in cfg["godmode"]:
            await self.free_voice(member, after)

        # persistent mute: keep them server-muted
        if after.channel and member.id in cfg["stfu"] and not after.mute:
            try:
                await member.edit(mute=True, reason="VC stfu")
            except discord.HTTPException:
                pass

        if not (changed and after.channel):
            return

        # autokick: only in voice channels whose owner turned it on, and only while the owner is in the call
        if after.channel.id in cfg["autokick_vcs"]:
            owner_id = self.vc_owner_id(after.channel)
            owner = guild.get_member(owner_id) if owner_id else None
            if (
                owner is not None
                and owner in after.channel.members
                and not self.autokick_exempt(after.channel, owner, member)
            ):
                await self.disconnect(member, f"Autokick by {owner}")
                return

        # followers move along with the person they follow
        for (gid, follower_id), targets in list(self.follows.items()):
            if gid != guild.id or member.id not in targets:
                continue
            follower = guild.get_member(follower_id)
            if (
                follower
                and follower.voice
                and follower.voice.channel
                and follower.voice.channel != after.channel
                and after.channel.permissions_for(follower).connect
            ):
                try:
                    await follower.move_to(after.channel, reason=f"Following {member}")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_ready(self):
        """Catch up on people already in voice when the bot (re)starts."""
        for guild in self.bot.guilds:
            cfg = self.cfg(guild)
            for channel in guild.voice_channels:
                for member in list(channel.members):
                    if member.bot:
                        continue
                    if member.id in cfg["blacklist"]:
                        await self.disconnect(member, "VC blacklist")
                    elif member.id in cfg["stfu"] and not member.voice.mute:
                        try:
                            await member.edit(mute=True, reason="VC stfu")
                        except discord.HTTPException:
                            pass
                    elif member.id in cfg["godmode"]:
                        await self.free_voice(member)

    # ==================================================================== commands

    @commands.group(name="vc", invoke_without_command=True)
    async def vc(self, ctx: commands.Context):
        """Voice utilities"""
        await ctx.send(f"Use `{ctx.prefix}help vc utility` to see every voice command.")

    # ------------------------------------------------------------- follow / chain

    async def _follow(self, ctx: commands.Context, member: discord.Member | None):
        key = (ctx.guild.id, ctx.author.id)
        if member is None:
            ids = self.follows.get(key)
            if not ids:
                return await ctx.send("You're not following anyone.")
            return await ctx.send(
                "You're following: " + ", ".join(f"<@{i}>" for i in ids), allowed_mentions=NO_PINGS
            )
        if member.id == ctx.author.id:
            return await ctx.send("You can't follow yourself.")
        if member.bot:
            return await ctx.send("You can't follow a bot.")
        if member.id in self.follows[key]:
            return await ctx.send(f"You're already following **{member.display_name}**.", allowed_mentions=NO_PINGS)

        moved = await self.add_follow(ctx.guild, ctx.author, member)
        text = f"You're now following **{member.display_name}**."
        if moved:
            text += " Moved you to their call."
        elif not (ctx.author.voice and ctx.author.voice.channel):
            text += " Join a voice channel and I'll move you whenever they switch."
        await ctx.send(text, allowed_mentions=NO_PINGS)

    async def _unfollow(self, ctx: commands.Context, who: str | None):
        key = (ctx.guild.id, ctx.author.id)
        if who is None or who.lower() == "all":
            if not self.follows.pop(key, None):
                return await ctx.send("You're not following anyone.")
            return await ctx.send("Stopped following everyone.")

        member = await commands.MemberConverter().convert(ctx, who)
        if member.id not in self.follows.get(key, set()):
            return await ctx.send(f"You're not following **{member.display_name}**.", allowed_mentions=NO_PINGS)
        self.follows[key].discard(member.id)
        if not self.follows[key]:
            del self.follows[key]
        await ctx.send(f"Stopped following **{member.display_name}**.", allowed_mentions=NO_PINGS)

    @vc.command(name="follow")
    async def vc_follow(self, ctx: commands.Context, member: discord.Member = None):
        """Follow and move to their call"""
        await self._follow(ctx, member)

    @commands.command(name="follow")
    async def follow(self, ctx: commands.Context, member: discord.Member = None):
        """Follow and move to their call"""
        await self._follow(ctx, member)

    @vc.command(name="unfollow")
    async def vc_unfollow(self, ctx: commands.Context, who: str = None):
        """Stop following"""
        await self._unfollow(ctx, who)

    @commands.command(name="unfollow")
    async def unfollow(self, ctx: commands.Context, who: str = None):
        """Stop following"""
        await self._unfollow(ctx, who)

    @vc.command(name="chain")
    async def vc_chain(self, ctx: commands.Context, members: commands.Greedy[discord.Member]):
        """Send chain requests"""
        members = [m for m in dict.fromkeys(members) if not m.bot and m.id != ctx.author.id][:5]
        if not members:
            return await ctx.send(f"Usage: `{ctx.prefix}vc chain @user @user ...`")

        for member in members:
            view = ChainRequest(self, ctx.author, member)
            view.message = await ctx.send(
                f"{member.mention}, **{ctx.author.display_name}** wants you in their chain. "
                "Accept to follow them between voice channels.",
                view=view,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )

    # ----------------------------------------------------- shields and protection

    @vc.command(name="shield")
    async def vc_shield(self, ctx: commands.Context, member: discord.Member):
        """Shield a member from being dragged or kicked"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        cfg = self.cfg(ctx.guild)
        if member.id in cfg["shield"]:
            return await ctx.send(f"**{member.display_name}** is already shielded.", allowed_mentions=NO_PINGS)
        cfg["shield"].append(member.id)
        self.store.save()
        await ctx.send(f"Shielded **{member.display_name}**.", allowed_mentions=NO_PINGS)

    @vc.command(name="unshield")
    async def vc_unshield(self, ctx: commands.Context, member: discord.Member):
        """Remove a member's shield"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        cfg = self.cfg(ctx.guild)
        if member.id not in cfg["shield"]:
            return await ctx.send(f"**{member.display_name}** isn't shielded.", allowed_mentions=NO_PINGS)
        cfg["shield"].remove(member.id)
        self.store.save()
        await ctx.send(f"Removed the shield from **{member.display_name}**.", allowed_mentions=NO_PINGS)

    @vc.command(name="antikick")
    async def vc_antikick(self, ctx: commands.Context):
        """Toggle your anti-kick protection"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        on = self.toggle(ctx.guild, "antikick", ctx.author.id)
        await ctx.send(f"Anti-kick is now **{'on' if on else 'off'}**.")

    @vc.command(name="antidrag")
    async def vc_antidrag(self, ctx: commands.Context):
        """Toggle your anti-drag protection"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        on = self.toggle(ctx.guild, "antidrag", ctx.author.id)
        await ctx.send(f"Anti-drag is now **{'on' if on else 'off'}**.")

    @vc.command(name="antikickdrag")
    async def vc_antikickdrag(self, ctx: commands.Context):
        """Toggle both anti-kick and anti-drag"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        cfg = self.cfg(ctx.guild)
        uid = ctx.author.id
        both_on = uid in cfg["antikick"] and uid in cfg["antidrag"]
        for key in ("antikick", "antidrag"):
            if both_on and uid in cfg[key]:
                cfg[key].remove(uid)
            elif not both_on and uid not in cfg[key]:
                cfg[key].append(uid)
        self.store.save()
        await ctx.send(f"Anti-kick and anti-drag are now **{'off' if both_on else 'on'}**.")

    # ---------------------------------------------------------------- drag / move

    @vc.command(name="drag")
    async def vc_drag(self, ctx: commands.Context, member: discord.Member):
        """Move a member into your call"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        if not await self.bot_can(ctx, "move_members", "Move Members"):
            return
        if not (ctx.author.voice and ctx.author.voice.channel):
            return await ctx.send("Join a voice channel first.")
        if not (member.voice and member.voice.channel):
            return await ctx.send(f"**{member.display_name}** isn't in a voice channel.", allowed_mentions=NO_PINGS)
        channel = ctx.author.voice.channel
        if member.voice.channel == channel:
            return await ctx.send(f"**{member.display_name}** is already in your call.", allowed_mentions=NO_PINGS)
        if not self.outranks(ctx.guild, ctx.author.id, member.id):
            return await ctx.send("You can't drag someone at or above your rank.")
        reason = self.protection(ctx.guild, ctx.author.id, member, "drag")
        if reason:
            return await ctx.send(f"**{member.display_name}** {reason}.", allowed_mentions=NO_PINGS)

        await member.move_to(channel, reason=f"Dragged by {ctx.author}")
        await ctx.send(f"Moved **{member.display_name}** to **{channel.name}**.", allowed_mentions=NO_PINGS)

    @vc.command(name="dragall")
    async def vc_dragall(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Move unprotected members from a channel into your call"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        if not await self.bot_can(ctx, "move_members", "Move Members"):
            return
        if not (ctx.author.voice and ctx.author.voice.channel):
            return await ctx.send("Join a voice channel first.")
        destination = ctx.author.voice.channel
        if channel == destination:
            return await ctx.send("That's the channel you're already in.")

        moved = skipped = 0
        for member in list(channel.members):
            if (
                not self.outranks(ctx.guild, ctx.author.id, member.id)
                or self.protection(ctx.guild, ctx.author.id, member, "drag")
            ):
                skipped += 1
                continue
            try:
                await member.move_to(destination, reason=f"Dragged by {ctx.author}")
                moved += 1
            except discord.HTTPException:
                skipped += 1

        text = f"Moved **{moved}** member(s) from **{channel.name}** to **{destination.name}**."
        if skipped:
            text += f" Skipped {skipped} (protected, higher rank, or couldn't be moved)."
        await ctx.send(text)

    @commands.command(name="moveall")
    async def moveall(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Move everyone from your current VC"""
        if not (ctx.author.guild_permissions.administrator or ctx.author.id == BOT_OWNER_ID):
            return await ctx.send("You need the **Administrator** permission to use that.")
        if not await self.bot_can(ctx, "move_members", "Move Members"):
            return
        if not (ctx.author.voice and ctx.author.voice.channel):
            return await ctx.send("Join a voice channel first.")
        source = ctx.author.voice.channel
        if channel == source:
            return await ctx.send("That's the channel you're already in.")

        moved = failed = 0
        for member in list(source.members):
            try:
                await member.move_to(channel, reason=f"Moveall by {ctx.author}")
                moved += 1
            except discord.HTTPException:
                failed += 1
        text = f"Moved **{moved}** member(s) to **{channel.name}**."
        if failed:
            text += f" {failed} couldn't be moved."
        await ctx.send(text)

    # -------------------------------------------------------------- voice history

    @vc.command(name="voicehistory")
    async def vc_voicehistory(self, ctx: commands.Context, member: discord.Member = None):
        """View voice history"""
        if not await self.need(ctx, LEVEL_CHEIF, "Cheif"):
            return
        events = self.history.get(ctx.guild.id)["events"]
        if member:
            events = [e for e in events if e[0] == member.id]
        events = events[-15:][::-1]
        if not events:
            return await ctx.send("No voice activity recorded yet.")

        lines = []
        for user_id, old, new, ts in events:
            if old and new:
                what = f"moved <#{old}> -> <#{new}>"
            elif new:
                what = f"joined <#{new}>"
            else:
                what = f"left <#{old}>"
            who = "" if member else f"<@{user_id}> "
            lines.append(f"<t:{ts}:R> {who}{what}")

        title = f"Voice history for {member.display_name}" if member else "Voice history"
        embed = discord.Embed(title=title, description="\n".join(lines), color=COLOR)
        embed.set_footer(text="Only activity since the bot started recording.")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    # ----------------------------------------------------------------- ownership

    async def rename_to_owner(self, channel: discord.VoiceChannel, member: discord.Member):
        """Name the channel after its new owner. A failed rename never blocks the claim."""
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

    @vc.command(name="forceclaim")
    async def vc_forceclaim(self, ctx: commands.Context):
        """Claim ownership of your current voice channel"""
        if not await self.need(ctx, LEVEL_CLOVER, "Clover"):
            return
        if not (ctx.author.voice and ctx.author.voice.channel):
            return await ctx.send("Join a voice channel first.")
        channel = ctx.author.voice.channel
        self.cfg(ctx.guild)["owners"][str(channel.id)] = ctx.author.id
        self.store.save()
        await self.rename_to_owner(channel, ctx.author)
        await ctx.send(f"You now own **{channel.name}**.")

    @vc.command(name="forceownership")
    async def vc_forceownership(self, ctx: commands.Context, member: discord.Member):
        """Assign ownership of your current voice channel"""
        if not await self.need(ctx, LEVEL_PRES, "Pres"):
            return
        voice = (ctx.author.voice or member.voice)
        channel = voice.channel if voice else None
        if channel is None:
            return await ctx.send("Join a voice channel first (or be in the same call as them).")
        self.cfg(ctx.guild)["owners"][str(channel.id)] = member.id
        self.store.save()
        await self.rename_to_owner(channel, member)
        await ctx.send(
            f"**{member.display_name}** now owns **{channel.name}**.", allowed_mentions=NO_PINGS
        )

    def vc_owner_id(self, channel) -> int | None:
        """Owner of a voice channel (VoiceMaster, `vc claim` and `vc forceclaim` owners all count)."""
        vc_cog = self.bot.get_cog("VC Commands")
        if vc_cog is not None:
            try:
                return vc_cog.owner_id(channel)
            except Exception:
                pass
        return self.cfg(channel.guild)["owners"].get(str(channel.id))

    def autokick_exempt(self, channel, owner: discord.Member, member: discord.Member) -> bool:
        """Members this channel's autokick never touches."""
        guild = channel.guild
        if member.id == owner.id or member.bot:
            return True
        if member.id in (guild.owner_id, BOT_OWNER_ID):
            return True
        if self.protection(guild, owner.id, member, "kick"):
            return True
        vc_cog = self.bot.get_cog("VC Commands")
        if vc_cog is not None:
            try:
                if member.id in vc_cog.partners(channel):  # co-owners
                    return True
            except Exception:
                pass
        return self.permitted_in(channel, member)

    def permitted_in(self, channel, member: discord.Member) -> bool:
        """True if the member was permitted to this call (`permit`) or has an explicit
        connect permission on it, so autokick should leave them alone."""
        vc = self.bot.get_cog("VC Commands")
        if vc is not None:
            try:
                if vc.is_permitted(channel.id, member.id):
                    return True
            except Exception:
                pass
        overwrite = channel.overwrites.get(member)
        return bool(overwrite is not None and overwrite.connect is True)

    # ------------------------------------------------------------------ autokick

    async def _autokick(self, ctx: commands.Context, state: str | None):
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            return await ctx.send("Join your voice channel first. Autokick works per voice channel.")
        if self.vc_owner_id(channel) != ctx.author.id:
            return await ctx.send("Only the owner of this voice channel can use autokick.")
        cfg = self.cfg(ctx.guild)
        on = channel.id in cfg["autokick_vcs"]
        if state is None:
            want = not on
        elif state.lower() in ("on", "enable", "true", "yes"):
            want = True
        elif state.lower() in ("off", "disable", "false", "no"):
            want = False
        else:
            return await ctx.send(f"Usage: `{ctx.prefix}autokick [on/off]`")

        if want and not on:
            cfg["autokick_vcs"].append(channel.id)
        elif not want and on:
            cfg["autokick_vcs"].remove(channel.id)
        self.store.save()
        await ctx.send(
            f"Autokick is now **{'on' if want else 'off'}** for {channel.mention}."
            + (f" Anyone who joins without a `{ctx.prefix}vc permit` gets disconnected while you're in the call." if want else "")
        )

    @vc.command(name="autokick")
    async def vc_autokick(self, ctx: commands.Context, state: str = None):
        """Toggle autokick"""
        await self._autokick(ctx, state)

    @commands.command(name="autokick")
    async def autokick(self, ctx: commands.Context, state: str = None):
        """Toggle autokick"""
        await self._autokick(ctx, state)

    # ---------------------------------------------------------------------- stfu

    @vc.group(name="stfu", invoke_without_command=True)
    async def vc_stfu(self, ctx: commands.Context, member: discord.Member = None):
        """Persistently voice-mute a member"""
        if member is None:
            return await ctx.send(f"Usage: `{ctx.prefix}vc stfu @user`")
        if not await self.need(ctx, LEVEL_PRES, "Pres"):
            return
        if not await self.bot_can(ctx, "mute_members", "Mute Members"):
            return
        if member.bot:
            return await ctx.send("You can't mute a bot.")
        if member.id in self.cfg(ctx.guild)["godmode"] and ctx.author.id != BOT_OWNER_ID:
            return await ctx.send(f"**{member.display_name}** has Godmode.", allowed_mentions=NO_PINGS)

        cfg = self.cfg(ctx.guild)
        if member.id in cfg["stfu"]:
            return await ctx.send(f"**{member.display_name}** is already muted.", allowed_mentions=NO_PINGS)
        cfg["stfu"].append(member.id)
        self.store.save()
        if member.voice and member.voice.channel:
            try:
                await member.edit(mute=True, reason=f"VC stfu by {ctx.author}")
            except discord.HTTPException:
                pass
        await ctx.send(
            f"**{member.display_name}** is now persistently muted in voice.", allowed_mentions=NO_PINGS
        )

    @vc_stfu.command(name="list")
    async def vc_stfu_list(self, ctx: commands.Context):
        """List persistent mutes"""
        if not await self.need(ctx, LEVEL_PRES, "Pres"):
            return
        ids = self.cfg(ctx.guild)["stfu"]
        embed = discord.Embed(
            title="Persistent voice mutes",
            description="\n".join(f"<@{i}> (`{i}`)" for i in ids) or "Nobody is muted.",
            color=COLOR,
        )
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @vc_stfu.command(name="wipeall")
    async def vc_stfu_wipeall(self, ctx: commands.Context):
        """Clear all persistent mutes"""
        if not await self.need(ctx, LEVEL_PRES, "Pres"):
            return
        cfg = self.cfg(ctx.guild)
        ids = list(cfg["stfu"])
        cfg["stfu"] = []
        self.store.save()
        for uid in ids:
            member = ctx.guild.get_member(uid)
            if member and member.voice and member.voice.mute:
                try:
                    await member.edit(mute=False, reason=f"VC stfu wipeall by {ctx.author}")
                except discord.HTTPException:
                    pass
        await ctx.send(f"Cleared **{len(ids)}** persistent mute(s).")

    @vc.command(name="unstfu")
    async def vc_unstfu(self, ctx: commands.Context, member: discord.Member):
        """Remove a persistent mute"""
        if not await self.need(ctx, LEVEL_PRES, "Pres"):
            return
        cfg = self.cfg(ctx.guild)
        if member.id not in cfg["stfu"]:
            return await ctx.send(f"**{member.display_name}** isn't muted.", allowed_mentions=NO_PINGS)
        cfg["stfu"].remove(member.id)
        self.store.save()
        if member.voice and member.voice.mute:
            try:
                await member.edit(mute=False, reason=f"VC unstfu by {ctx.author}")
            except discord.HTTPException:
                pass
        await ctx.send(f"Unmuted **{member.display_name}**.", allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------------- godmode

    async def _godmode(self, ctx: commands.Context, member: discord.Member, force_off: bool = False):
        if not await self.need(ctx, LEVEL_GOD, "God"):
            return
        cfg = self.cfg(ctx.guild)
        has = member.id in cfg["godmode"]
        granted = False
        if force_off:
            if not has:
                return await ctx.send(f"**{member.display_name}** doesn't have Godmode.", allowed_mentions=NO_PINGS)
            cfg["godmode"].remove(member.id)
            msg = f"Removed Godmode from **{member.display_name}**."
        elif has:
            cfg["godmode"].remove(member.id)
            msg = f"Removed Godmode from **{member.display_name}**."
        else:
            cfg["godmode"].append(member.id)
            msg = f"**{member.display_name}** now has Godmode."
            granted = True
        self.store.save()
        if granted:
            await self.free_voice(member)  # clear any mute/deafen they already have
        await ctx.send(msg, allowed_mentions=NO_PINGS)

    @commands.group(name="godmode", invoke_without_command=True)
    async def godmode(self, ctx: commands.Context, member: discord.Member = None):
        """Toggle Godmode protection"""
        if member is None:
            return await ctx.send(f"Usage: `{ctx.prefix}godmode @user`")
        await self._godmode(ctx, member)

    @godmode.command(name="list")
    async def godmode_list(self, ctx: commands.Context):
        """List Godmode"""
        if not await self.need(ctx, LEVEL_FOOL, "Fool"):
            return
        ids = self.cfg(ctx.guild)["godmode"]
        embed = discord.Embed(
            title="Godmode",
            description="\n".join(f"<@{i}> (`{i}`)" for i in ids) or "Nobody has Godmode.",
            color=COLOR,
        )
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @godmode.command(name="wipeall")
    async def godmode_wipeall(self, ctx: commands.Context):
        """Clear Godmode"""
        if not await self.need(ctx, LEVEL_FOOL, "Fool"):
            return
        cfg = self.cfg(ctx.guild)
        count = len(cfg["godmode"])
        cfg["godmode"] = []
        self.store.save()
        await ctx.send(f"Cleared Godmode for **{count}** member(s).")

    @vc.command(name="godmode")
    async def vc_godmode(self, ctx: commands.Context, member: discord.Member):
        """Toggle Godmode protection"""
        await self._godmode(ctx, member)

    @vc.command(name="ungodmode")
    async def vc_ungodmode(self, ctx: commands.Context, member: discord.Member):
        """Remove Godmode protection"""
        await self._godmode(ctx, member, force_off=True)

    # ----------------------------------------------------------------- blacklist

    async def _sunset(self, ctx: commands.Context) -> bool:
        if self.level(ctx.guild, ctx.author.id) >= LEVEL_SERVER_OWNER:
            return True
        await ctx.send("Only the server owner or bot owner can use the voice blacklist.")
        return False

    @vc.group(name="blacklist", invoke_without_command=True)
    async def vc_blacklist(self, ctx: commands.Context, member: discord.Member = None):
        """Blacklist a member from voice"""
        if member is None:
            return await ctx.send(f"Usage: `{ctx.prefix}vc blacklist @user`")
        if not await self._sunset(ctx):
            return
        if member.bot or member.id in (BOT_OWNER_ID, ctx.guild.owner_id):
            return await ctx.send("You can't blacklist that member.")
        cfg = self.cfg(ctx.guild)
        if member.id in cfg["blacklist"]:
            return await ctx.send(f"**{member.display_name}** is already blacklisted.", allowed_mentions=NO_PINGS)
        cfg["blacklist"].append(member.id)
        self.store.save()
        if member.voice and member.voice.channel:
            await self.disconnect(member, f"VC blacklist by {ctx.author}")
        await ctx.send(
            f"**{member.display_name}** is blacklisted from voice.", allowed_mentions=NO_PINGS
        )

    @vc_blacklist.command(name="remove")
    async def vc_blacklist_remove(self, ctx: commands.Context, member: discord.Member):
        """Remove a member from the voice blacklist"""
        if not await self._sunset(ctx):
            return
        cfg = self.cfg(ctx.guild)
        if member.id not in cfg["blacklist"]:
            return await ctx.send(f"**{member.display_name}** isn't blacklisted.", allowed_mentions=NO_PINGS)
        cfg["blacklist"].remove(member.id)
        self.store.save()
        await ctx.send(
            f"Removed **{member.display_name}** from the voice blacklist.", allowed_mentions=NO_PINGS
        )

    @vc_blacklist.command(name="list")
    async def vc_blacklist_list(self, ctx: commands.Context):
        """List blacklisted members"""
        if not await self._sunset(ctx):
            return
        ids = self.cfg(ctx.guild)["blacklist"]
        embed = discord.Embed(
            title="Voice blacklist",
            description="\n".join(f"<@{i}> (`{i}`)" for i in ids) or "Nobody is blacklisted.",
            color=COLOR,
        )
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """Forget autokick for voice channels that no longer exist."""
        if isinstance(channel, discord.VoiceChannel):
            cfg = self.cfg(channel.guild)
            if channel.id in cfg["autokick_vcs"]:
                cfg["autokick_vcs"].remove(channel.id)
                self.store.save()


async def setup(bot: commands.Bot):
    await bot.add_cog(VCUtility(bot))
