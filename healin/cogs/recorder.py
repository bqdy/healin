"""Recorder cog: record a voice channel and post the result as an mp3 when it stops.

Needs (see requirements.txt):
  * discord-ext-voice-recv - lets the bot *receive* voice (plain discord.py can only send it)
  * PyNaCl and FFmpeg      - same as the music cog; FFmpeg mixes everyone into one mp3

Everyone in the call is mixed into a single mp3. The bot announces the recording in the text channel
when it starts, and again whenever someone joins the call while it is running.
Only one recording per server, and it stops by itself after MAX_SECONDS or when everyone has left.
"""
import asyncio
import os
import shutil
import tempfile
import threading
import time
import traceback
import wave
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands

from utils.base import COLOR, NO_PINGS, BaseCog
from utils.safe_cog import add_cog_safely

try:
    from discord.ext import voice_recv
except ImportError:  # the cog still loads; commands explain what's missing
    voice_recv = None
try:
    import nacl  # noqa: F401
except ImportError:
    nacl = None

try:
    from cogs.dev import BOT_OWNER_ID
except ImportError:
    BOT_OWNER_ID = 482506940027502597

RATE = 48000                  # Discord voice: 48 kHz, 16-bit, stereo
CHANNELS = 2
WIDTH = 2
BYTES_PER_SEC = RATE * CHANNELS * WIDTH
MAX_SECONDS = 3600            # a recording stops by itself after an hour (raw audio takes disk space)
EMPTY_TIMEOUT = 60            # seconds with nobody in the call before it stops by itself
GAP_TOLERANCE = 0.12          # silence shorter than this isn't padded (it's just network jitter)
DAVE_HINT = ("Discord now encrypts voice calls end to end (DAVE) and the standard `discord-ext-voice-recv` "
             "release can't decrypt them, so only a moment of audio gets through. The bot owner needs a "
             "DAVE-capable build of the library (see the notes in requirements.txt).")
MP3_BITRATES = ("128k", "64k", "32k")  # tried in order until the file fits Discord's upload limit


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_name(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in text).strip("-")
    return cleaned[:40] or "call"


class Session:
    """One recording. Audio arrives on a network thread, so everything touching files is locked."""

    def __init__(self, guild: discord.Guild, channel, text_channel, started_by: discord.Member, folder: str):
        self.guild = guild
        self.channel = channel
        self.text_channel = text_channel
        self.started_by = started_by
        self.folder = folder
        self.started = time.monotonic()
        self.started_at = datetime.now()
        self.files: dict = {}        # user id -> [wave writer, bytes written, path]
        self.lock = threading.Lock()
        self.closed = False
        self.watch_task: Optional[asyncio.Task] = None
        self.stopping = False
        self.pcm_bytes = 0           # total audio captured, to spot a recording that got (almost) nothing
        self.packets = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def add(self, user_id, pcm: bytes):
        """Store one chunk of a user's audio, padding with silence for the time they were quiet."""
        if not pcm:
            return
        with self.lock:
            if self.closed:
                return
            entry = self.files.get(user_id)
            if entry is None:
                path = os.path.join(self.folder, f"{user_id}.wav")
                writer = wave.open(path, "wb")
                writer.setnchannels(CHANNELS)
                writer.setsampwidth(WIDTH)
                writer.setframerate(RATE)
                entry = self.files[user_id] = [writer, 0, path]
            writer, written, _ = entry
            # where this chunk should start on the recording's timeline
            start = int((self.elapsed - len(pcm) / BYTES_PER_SEC) * BYTES_PER_SEC)
            gap = start - written
            if gap > GAP_TOLERANCE * BYTES_PER_SEC:
                gap -= gap % (CHANNELS * WIDTH)
                writer.writeframes(b"\x00" * gap)
                written += gap
            writer.writeframes(pcm)
            entry[1] = written + len(pcm)
            self.pcm_bytes += len(pcm)
            self.packets += 1

    def close(self) -> list:
        """Stop taking audio and return the wav files that were written."""
        with self.lock:
            self.closed = True
            paths = []
            for writer, _, path in self.files.values():
                try:
                    writer.close()
                except Exception:
                    pass
                paths.append(path)
        return paths


async def run_ffmpeg(*args) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"[recorder] ffmpeg failed: {err.decode(errors='replace')[:300]}")
    return proc.returncode == 0


async def mix_to_mp3(wavs: list, out_path: str, bitrate: str) -> bool:
    """Mix every user's wav into one mp3."""
    inputs = []
    for path in wavs:
        inputs += ["-i", path]
    if len(wavs) == 1:
        return await run_ffmpeg(*inputs, "-ac", "2", "-b:a", bitrate, out_path)
    labels = "".join(f"[{i}:a]" for i in range(len(wavs)))
    # normalize=0 keeps each person at their real volume (needs ffmpeg 4.4+; older builds get the default mix)
    for opts in ("duration=longest:normalize=0", "duration=longest"):
        flt = f"{labels}amix=inputs={len(wavs)}:{opts}"
        if await run_ffmpeg(*inputs, "-filter_complex", flt, "-ac", "2", "-b:a", bitrate, out_path):
            return True
    return False


if voice_recv is not None:

    class RecordSink(voice_recv.AudioSink):
        """Hands each user's decoded audio to the Session."""

        def __init__(self, session: Session):
            super().__init__()
            self.session = session
            self.reported = False

        def wants_opus(self) -> bool:
            return False  # we want decoded PCM

        def write(self, user, data):
            try:
                if getattr(user, "bot", False):
                    return
                if user is not None:
                    key = user.id
                else:
                    # the library doesn't know who this is yet (first packets of a new speaker): keep the
                    # audio under its SSRC instead of dropping it
                    key = f"ssrc-{getattr(getattr(data, 'packet', None), 'ssrc', 'unknown')}"
                self.session.add(key, data.pcm)
            except Exception:
                if not self.reported:
                    self.reported = True
                    traceback.print_exc()  # an exception here could otherwise stop the whole receive loop

        def cleanup(self):
            pass


class Recorder(BaseCog, name="Recorder"):
    """Record a voice channel and get an mp3 back"""

    help_text = (
        "# Recorder\n\n"
        "**Required:** Manage Server, Anti-Nuke admin, or server owner. You must be in the voice channel.\n\n"
        "`{p}record start` - Join your voice channel and start recording everyone in it\n"
        "`{p}record stop` - Stop recording and post the mp3 (alias `end`)\n"
        "`{p}record status` - See how long the current recording has been running\n\n"
        "The bot announces the recording in this channel when it starts and whenever someone joins the "
        "call. Recordings stop by themselves after an hour or once everyone has left."
    )

    def __init__(self, bot):
        self.bot = bot
        self.sessions: dict = {}  # guild id -> Session
        self.base = os.path.join(os.getenv("DATA_DIR", "data"), "recordings")
        os.makedirs(self.base, exist_ok=True)

    async def cog_unload(self):
        for guild_id in list(self.sessions):
            await self.finish(guild_id, "The recorder was reloaded.")

    # ---- helpers ------------------------------------------------------------
    def is_recording(self, guild: discord.Guild) -> bool:
        return guild.id in self.sessions

    async def ready_check(self, ctx) -> bool:
        if voice_recv is None:
            await ctx.send(
                "Recording isn't set up yet: `discord-ext-voice-recv` isn't installed "
                "(`pip install discord-ext-voice-recv`, see requirements.txt)."
            )
            return False
        if nacl is None:
            await ctx.send("Recording isn't set up yet: `PyNaCl` isn't installed (see requirements.txt).")
            return False
        if shutil.which("ffmpeg") is None:
            await ctx.send("Recording isn't set up yet: FFmpeg isn't installed on the machine I run on.")
            return False
        return True

    def allowed(self, member: discord.Member) -> bool:
        if member.id in (member.guild.owner_id, BOT_OWNER_ID):
            return True
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            return True
        antinuke = self.bot.get_cog("AntiNuke")
        return bool(antinuke and member.id in antinuke.cfg(member.guild)["admins"])

    async def say(self, session: Session, *args, **kwargs):
        try:
            await session.text_channel.send(*args, allowed_mentions=NO_PINGS, **kwargs)
        except discord.HTTPException:
            pass

    # ---- finishing a recording ------------------------------------------------
    async def finish(self, guild_id: int, reason: Optional[str] = None):
        """Stop recording, mix everything into an mp3 and post it. Safe to call more than once."""
        session = self.sessions.pop(guild_id, None)
        if session is None or session.stopping:
            return
        session.stopping = True
        if session.watch_task and session.watch_task is not asyncio.current_task():
            session.watch_task.cancel()

        vc = session.guild.voice_client
        if vc is not None:
            try:
                vc.stop_listening()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

        duration = session.elapsed
        wavs = session.close()
        captured = session.pcm_bytes / BYTES_PER_SEC
        print(f"[recorder] {session.guild.id}: {session.packets} audio chunks, {captured:.1f}s of speech "
              f"in {duration:.1f}s, {len(wavs)} speaker(s)")
        try:
            await self.deliver(session, wavs, duration, reason)
        except Exception:
            traceback.print_exc()
            await self.say(session, "❌ Something went wrong while saving the recording.")
        finally:
            shutil.rmtree(session.folder, ignore_errors=True)

    async def deliver(self, session: Session, wavs: list, duration: float, reason: Optional[str]):
        head = f"⏹️ Recording stopped after **{fmt_duration(duration)}**."
        if reason:
            head = f"⏹️ {reason} Recording ended after **{fmt_duration(duration)}**."
        if not wavs:
            return await self.say(
                session,
                f"{head} I didn't receive any audio. If people were talking, see the note on Discord's "
                f"encrypted calls: {DAVE_HINT}",
            )

        limit = session.guild.filesize_limit
        stamp = session.started_at.strftime("%Y-%m-%d_%H-%M")
        name = f"recording-{safe_name(session.channel.name)}-{stamp}.mp3"
        out = os.path.join(session.folder, name)

        async with session.text_channel.typing():
            for bitrate in MP3_BITRATES:
                if not await mix_to_mp3(wavs, out, bitrate):
                    return await self.say(session, f"{head} ❌ FFmpeg couldn't turn it into an mp3.")
                if os.path.getsize(out) <= limit:
                    break
            else:
                # too big for Discord even at the lowest bitrate: keep it on the host instead
                keep = os.path.join(self.base, name)
                shutil.move(out, keep)
                return await self.say(
                    session,
                    f"{head} The mp3 is too large to upload here, so it was saved on the bot's host as `{keep}`.",
                )
            note = ""
            if duration >= 20 and captured < duration * 0.1:
                note = f"\n⚠️ Only {fmt_duration(captured)} of speech was captured. {DAVE_HINT}"
            await session.text_channel.send(
                f"{head} {len(wavs)} speaker(s) recorded.{note}",
                file=discord.File(out, filename=name),
                allowed_mentions=NO_PINGS,
            )

    async def watch(self, session: Session):
        """Stops the recording on its own after MAX_SECONDS or when the call is empty."""
        empty_since = None
        try:
            while True:
                await asyncio.sleep(10)
                if session.elapsed >= MAX_SECONDS:
                    return await self.finish(session.guild.id, "Reached the 1 hour limit.")
                humans = [m for m in session.channel.members if not m.bot]
                if humans:
                    empty_since = None
                elif empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since >= EMPTY_TIMEOUT:
                    return await self.finish(session.guild.id, "Everyone left the call.")
        except asyncio.CancelledError:
            pass

    # ---- commands ---------------------------------------------------------------
    @commands.group(name="record", aliases=["rec"], invoke_without_command=True)
    async def record(self, ctx):
        """Record a voice channel"""
        await ctx.send(f"Usage: `{ctx.clean_prefix}record start`, `{ctx.clean_prefix}record stop` or `{ctx.clean_prefix}record status`")

    @record.command(name="start")
    async def record_start(self, ctx):
        """Join your voice channel and start recording"""
        if not self.allowed(ctx.author):
            return await ctx.send("You need Manage Server (or to be an Anti-Nuke admin) to record.")
        if not await self.ready_check(ctx):
            return
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            return await ctx.send("Join a voice channel first.")
        if ctx.guild.id in self.sessions:
            where = self.sessions[ctx.guild.id].channel.mention
            return await ctx.send(f"I'm already recording {where}. Use `{ctx.clean_prefix}record stop` first.")
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.connect:
            return await ctx.send(f"I need the Connect permission in {channel.mention}.")
        if ctx.guild.voice_client is not None:
            return await ctx.send("I'm already in a voice channel (music?). Make me leave first, then try again.")

        try:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
        except Exception as e:
            return await ctx.send(f"❌ I couldn't join the voice channel: {str(e)[:200] or e.__class__.__name__}")

        folder = tempfile.mkdtemp(prefix="rec-", dir=self.base)
        session = Session(ctx.guild, channel, ctx.channel, ctx.author, folder)
        self.sessions[ctx.guild.id] = session
        try:
            vc.listen(RecordSink(session))
        except Exception as e:
            self.sessions.pop(ctx.guild.id, None)
            shutil.rmtree(folder, ignore_errors=True)
            await vc.disconnect(force=True)
            return await ctx.send(f"❌ I couldn't start listening: {str(e)[:200] or e.__class__.__name__}")

        session.watch_task = asyncio.create_task(self.watch(session))
        await ctx.send(
            f"🔴 **Recording {channel.mention}.** Everyone in this call is being recorded. "
            f"Stop with `{ctx.clean_prefix}record stop`, or leave the call if you don't want to be recorded.",
            allowed_mentions=NO_PINGS,
        )

    @record.command(name="stop", aliases=["end"])
    async def record_stop(self, ctx):
        """Stop recording and post the mp3"""
        session = self.sessions.get(ctx.guild.id)
        if session is None:
            return await ctx.send("I'm not recording anything.")
        if not (self.allowed(ctx.author) or ctx.author.id == session.started_by.id):
            return await ctx.send("Only the person who started the recording, or someone with Manage Server, can stop it.")
        session.text_channel = ctx.channel  # the mp3 is posted where it was stopped
        await self.finish(ctx.guild.id)

    @record.command(name="status")
    async def record_status(self, ctx):
        """See the current recording"""
        session = self.sessions.get(ctx.guild.id)
        if session is None:
            return await ctx.send("I'm not recording anything.")
        left = max(0, MAX_SECONDS - session.elapsed)
        embed = discord.Embed(title="🔴 Recording", color=COLOR)
        embed.add_field(name="Channel", value=session.channel.mention)
        embed.add_field(name="Running for", value=fmt_duration(session.elapsed))
        embed.add_field(name="Stops automatically in", value=fmt_duration(left))
        embed.add_field(name="Started by", value=session.started_by.display_name)
        await ctx.send(embed=embed)

    # ---- voice events -----------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        session = self.sessions.get(member.guild.id)
        if session is None:
            return
        if member.id == self.bot.user.id:
            if after.channel is None:  # kicked or disconnected: save what we have
                await self.finish(member.guild.id, "I was disconnected from the call.")
            return
        # tell people who walk into a call that is being recorded
        if after.channel == session.channel and before.channel != session.channel and not member.bot:
            await self.say(
                session,
                f"🔴 **{discord.utils.escape_markdown(member.display_name)}** joined {session.channel.mention}, "
                "which is being recorded.",
            )


async def setup(bot):
    add_cog_safely(bot, Recorder(bot))
