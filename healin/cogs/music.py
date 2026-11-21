"""Music cog: play songs from YouTube (and anything else yt-dlp supports) in a voice channel.

Needs three things on the machine running the bot (see requirements.txt):
  * yt-dlp   - finds the audio        (pip install yt-dlp)
  * PyNaCl   - lets discord.py talk in voice (pip install PyNaCl)
  * FFmpeg   - a program, not a pip package; install it with your system's package manager

Each server gets its own queue. The bot leaves after 5 minutes of nothing playing, or a minute after
everyone has left the voice channel.
"""
import asyncio
import html
import ipaddress
import json
import os
import random
import re
import shutil
import tempfile
import time
import posixpath
import urllib.parse
import urllib.request
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands

from utils.base import COLOR, NO_PINGS, BaseCog
from utils.paginator import chunk_pages, paginate
from utils.safe_cog import add_cog_safely

try:
    import yt_dlp
except ImportError:  # the cog still loads; commands explain what's missing
    yt_dlp = None
try:
    import nacl  # noqa: F401
except ImportError:
    nacl = None

IDLE_TIMEOUT = 300      # seconds with nothing to play before the bot leaves
EMPTY_TIMEOUT = 60      # seconds alone in the channel before the bot leaves
MAX_QUEUE = 200
MAX_PLAYLIST = 50       # songs taken from one playlist link
URL_TTL = 600           # stream links expire, so they're looked up again when older than this
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
# audio (and video, played as audio) files that can be uploaded or linked directly
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma", ".aiff", ".aif",
              ".amr", ".mka", ".webm", ".mp4", ".mov", ".mkv"}
# direct files may only be fetched over plain web protocols, so a playlist file can't point ffmpeg at local files
FFMPEG_DIRECT_BEFORE = ("-protocol_whitelist http,https,tcp,tls,crypto "
                        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
LOOP_MODES = {"off": "off", "none": "off", "track": "track", "song": "track", "one": "track",
              "queue": "queue", "all": "queue"}


def ensure_opus() -> bool:
    """discord.py needs the Opus library to send audio. Try the usual names if it didn't load itself."""
    if discord.opus.is_loaded():
        return True
    for name in ("libopus.so.0", "libopus.so", "libopus.0.dylib", "opus"):
        try:
            discord.opus.load_opus(name)
            return True
        except Exception:
            continue
    return False


# ---- small pure helpers --------------------------------------------------------
def fmt_time(seconds) -> str:
    if seconds is None:
        return "live"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://\S+$", text.strip()))


def wants_playlist(url: str) -> bool:
    """A pure playlist link, as opposed to a video link that happens to carry a playlist id."""
    path = urlparse(url).path
    if "/playlist" in path or "/sets/" in path:  # YouTube playlists and SoundCloud sets
        return True
    return bool(re.search(r"[?&]list=", url)) and not re.search(r"[?&]v=|youtu\.be/", url)


def clean_error(e) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    if "Sign in to confirm" in text:
        return ("YouTube is blocking this host and asking it to prove it isn't a bot. "
                "The bot owner needs to put a `cookies.txt` file next to main.py.")
    if "DRM" in text:
        return ("that site uses DRM protection, so I can't play links from it. "
                "Try the song's name instead, or a YouTube / SoundCloud link.")
    text = re.sub(r"^(ERROR:\s*)+", "", text)
    return (text or e.__class__.__name__)[:200]


SPOTIFY_LINK = re.compile(r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)", re.I)
APPLE_LINK = re.compile(r"https?://(?:embed\.)?music\.apple\.com/", re.I)


# Spotify and Apple Music audio is DRM protected and is never touched. For their links we only read the
# public song and artist names off the page, then search YouTube for the same songs.
PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


async def fetch_page(url: str) -> Optional[str]:
    try:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, headers=PAGE_HEADERS) as resp:
                if resp.status != 200:
                    print(f"[music] {url} answered HTTP {resp.status}")
                    return None
                return await resp.text()
    except Exception as e:
        print(f"[music] couldn't fetch {url}: {e!r}")
        return None


def meta_tag(page: str, prop: str) -> str:
    for tag in re.findall(r"<meta[^>]*>", page or "", re.I):
        if re.search(rf'(?:property|name)="{re.escape(prop)}"', tag, re.I):
            m = re.search(r'content="([^"]*)"', tag, re.I)
            if m:
                return html.unescape(m.group(1)).strip()
    return ""


def embed_entity(page: Optional[str]) -> Optional[dict]:
    """The data Spotify's embed page carries (title, subtitle, trackList ...)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, ValueError):
        return None


def unique_pairs(pairs: list) -> list:
    seen, out = set(), []
    for artist, title in pairs:
        key = (artist.lower(), title.lower())
        if title and key not in seen:
            seen.add(key)
            out.append((artist, title))
    return out


async def spotify_pairs(match) -> list:
    """[(artist, title), ...] for a Spotify track, album or playlist link."""
    kind, sid = match.group(1).lower(), match.group(2)
    entity = embed_entity(await fetch_page(f"https://open.spotify.com/embed/{kind}/{sid}"))

    if kind != "track":  # albums and playlists: the embed page lists the tracks
        if not entity:
            return []
        return unique_pairs(
            [((t.get("subtitle") or "").replace("\xa0", " "), t.get("title") or "")
             for t in entity.get("trackList", []) if isinstance(t, dict)]
        )

    # a single track: the embed data first, then the normal page's tags
    if entity:
        title = entity.get("title") or entity.get("name") or ""
        artist = (entity.get("subtitle") or "").replace("\xa0", " ")
        if not artist and isinstance(entity.get("artists"), list):
            artist = ", ".join(a.get("name", "") for a in entity["artists"] if isinstance(a, dict))
        if title and artist:
            return [(artist, title)]

    page = await fetch_page(f"https://open.spotify.com/track/{sid}")
    if not page:
        return []
    m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    m = m and re.match(r"(.+?) - song (?:and lyrics )?by (.+?) \| Spotify", html.unescape(m.group(1)).strip())
    if m:
        return [(m.group(2), m.group(1))]
    title, desc = meta_tag(page, "og:title"), meta_tag(page, "og:description")
    if title and " · " in desc:
        return [(desc.split(" · ")[0].strip(), title)]
    print(f"[music] couldn't find song and artist on the Spotify page for track {sid}")
    return []


def _ld_artist(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("name")
    return value if isinstance(value, str) else ""


def _walk_ld(obj, artist: str, pairs: list):
    if isinstance(obj, list):
        for item in obj:
            _walk_ld(item, artist, pairs)
    elif isinstance(obj, dict):
        artist = _ld_artist(obj.get("byArtist")) or artist
        if obj.get("@type") == "MusicRecording" and obj.get("name"):
            pairs.append((artist, obj["name"]))
        for key in ("tracks", "track"):
            if key in obj:
                _walk_ld(obj[key], artist, pairs)


async def apple_pairs(url: str) -> list:
    """[(artist, title), ...] for an Apple Music song, album or playlist link."""
    page = await fetch_page(url)
    if not page:
        return []
    if re.search(r"[?&]i=\d+", url):  # one song inside an album link
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        title = html.unescape(m.group(1)).replace("\u200e", "").strip() if m else ""
        m = re.match(r"(.+?) - Song by (.+?) - Apple Music", title)
        if m:
            return [(m.group(2), m.group(1))]
    pairs: list = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            _walk_ld(json.loads(block), "", pairs)
        except ValueError:
            continue
    return unique_pairs(pairs)


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text)


def file_ext(name: str) -> str:
    return posixpath.splitext(urlparse(name).path or name)[1].lower()


def is_audio_url(url: str) -> bool:
    """A direct link to an audio file (e.g. https://host/song.mp3 or a Discord attachment link)."""
    return is_url(url) and file_ext(url) in AUDIO_EXTS


def is_public_url(url: str) -> bool:
    """Refuse links that point at this machine or a private network."""
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)


def attachment_is_audio(att: discord.Attachment) -> bool:
    ctype = (att.content_type or "").lower()
    return ctype.startswith(("audio/", "video/")) or file_ext(att.filename) in AUDIO_EXTS


def file_title(name: str) -> str:
    base = posixpath.splitext(urllib.parse.unquote(posixpath.basename(urlparse(name).path or name)))[0]
    return base.replace("_", " ").strip() or "Audio file"


async def probe_duration(url: str) -> Optional[int]:
    """Length of a direct file in seconds using ffprobe (None if it can't be read)."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-protocol_whitelist", "http,https,tcp,tls,crypto",
            "-show_entries", "format=duration", "-of", "csv=p=0", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        return int(float(out.decode().strip().splitlines()[0]))
    except Exception:
        return None


@dataclass
class Track:
    title: str
    page_url: str
    requester: str                 # a display name only, so nothing here can ping anyone
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    stream_url: Optional[str] = None
    resolved_at: float = 0.0
    direct: bool = False           # an uploaded or linked audio file: the link is the audio, nothing to look up

    @property
    def length(self) -> str:
        if self.duration is None and self.direct:
            return "unknown"
        return fmt_time(self.duration)


def track_from_info(info: dict, requester: str) -> Optional[Track]:
    page = info.get("webpage_url") or info.get("original_url") or info.get("url")
    if not page:
        return None
    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    track = Track(title=info.get("title") or "Unknown title", page_url=page, requester=requester,
                  duration=info.get("duration"), thumbnail=thumb)
    # a fully looked-up video carries its audio link; a bare playlist entry doesn't
    if (info.get("formats") or info.get("requested_formats")) and info.get("url"):
        track.stream_url = info["url"]
        track.resolved_at = time.time()
    return track


def _extract(query: str, playlist: bool = False, clients: Optional[list] = None):
    """One blocking yt-dlp lookup."""
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": not playlist,
        "source_address": "0.0.0.0",  # IPv4 only; IPv6 often gets blocked
    }
    if playlist:
        opts.update(extract_flat="in_playlist", playlistend=MAX_PLAYLIST)
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}

    # YouTube blocks many hosting IPs with "Sign in to confirm you're not a bot". A cookies.txt exported
    # from a logged-in browser fixes that (path can be changed with YT_COOKIES in .env).
    # yt-dlp rewrites its cookie file, so each lookup gets its own copy.
    cookies = os.getenv("YT_COOKIES", "cookies.txt")
    tmp = None
    if os.path.isfile(cookies):
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        shutil.copyfile(cookies, tmp)
        opts["cookiefile"] = tmp
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def is_bot_check(e: Exception) -> bool:
    return "Sign in to confirm" in str(e)


def youtube_title(url: str) -> Optional[str]:
    """A YouTube video's title and channel, from its public oEmbed data (no sign-in needed)."""
    if not re.search(r"youtube\.com/watch|youtu\.be/", url):
        return None
    try:
        api = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(api, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        title = re.sub(r"\s*[\(\[](official|lyric|audio|music|video|hd|4k)[^\)\]]*[\)\]]", "", data.get("title", ""), flags=re.I)
        return title.strip() or None
    except Exception:
        return None


def extract(query: str, playlist: bool = False):
    """Blocking lookup, run in a thread. When YouTube refuses with its "not a bot" check it tries other
    YouTube clients, and then finds the same song on SoundCloud so playback still works."""
    try:
        return _extract(query, playlist)
    except yt_dlp.utils.DownloadError as e:
        if not is_bot_check(e):
            raise
        first = e
    try:
        return _extract(query, playlist, clients=["android_vr", "tv", "web_safari"])
    except yt_dlp.utils.DownloadError as e:
        if not is_bot_check(e):
            raise

    if query.startswith("ytsearch"):
        search = query.split(":", 1)[1]
    else:
        search = youtube_title(query)
    if search:
        print(f"[music] YouTube blocked this host, using SoundCloud for: {search}")
        try:
            return _extract(f"scsearch1:{search}", False)
        except yt_dlp.utils.DownloadError:
            pass
    raise first


# ---- one player per server ------------------------------------------------------
class GuildPlayer:
    def __init__(self, cog: "Music", guild: discord.Guild, channel):
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.channel = channel              # where "now playing" messages go
        self.queue: deque = deque()
        self.current: Optional[Track] = None
        self.volume = 0.5
        self.loop = "off"                   # off / track / queue
        self.skipped = False
        self.closed = False
        self.done = asyncio.Event()         # set when the current song ends
        self.wake = asyncio.Event()         # set when songs are added
        self.empty_task: Optional[asyncio.Task] = None
        self.task = asyncio.create_task(self.run())

    async def say(self, *args, **kwargs):
        try:
            await self.channel.send(*args, allowed_mentions=NO_PINGS, **kwargs)
        except discord.HTTPException:
            pass

    def after(self, error):  # called from ffmpeg's thread
        if error:
            print(f"[music] playback error in {self.guild.id}: {error!r}")
        self.bot.loop.call_soon_threadsafe(self.done.set)

    async def make_source(self, track: Track):
        await self.cog.resolve(track)
        before = FFMPEG_DIRECT_BEFORE if track.direct else FFMPEG_BEFORE
        audio = discord.FFmpegPCMAudio(track.stream_url, before_options=before, options="-vn")
        return discord.PCMVolumeTransformer(audio, volume=self.volume)

    def now_playing_embed(self, track: Track, title="Now playing") -> discord.Embed:
        label = track.title.replace("[", "(").replace("]", ")")
        e = discord.Embed(title=title, description=f"**[{label}]({track.page_url})**", color=COLOR)
        e.add_field(name="Length", value=track.length)
        e.add_field(name="Requested by", value=esc(track.requester))
        if track.thumbnail:
            e.set_thumbnail(url=track.thumbnail)
        return e

    async def run(self):
        try:
            while not self.closed:
                if not self.queue:
                    self.current = None
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        await self.destroy("👋 Left the voice channel after 5 minutes of nothing playing.")
                        return
                    continue
                track = self.queue.popleft()
                self.current = track
                vc = self.guild.voice_client
                if vc is None or not vc.is_connected():
                    await self.destroy()
                    return
                try:
                    source = await self.make_source(track)
                except Exception as e:
                    await self.say(f"❌ Couldn't play **{esc(track.title)}**: {clean_error(e)}")
                    continue
                self.done.clear()
                self.skipped = False
                started = time.monotonic()
                try:
                    vc.play(source, after=self.after)
                except Exception as e:
                    await self.say(f"❌ Couldn't play **{esc(track.title)}**: {clean_error(e)}")
                    continue
                await self.say(embed=self.now_playing_embed(track))
                await self.done.wait()
                if not self.skipped and time.monotonic() - started < 2:
                    # ended almost instantly: the stream is broken, so don't loop it forever
                    await self.say(f"⚠️ **{esc(track.title)}** stopped right away, skipping it.")
                    continue
                if self.loop == "track" and not self.skipped:
                    self.queue.appendleft(track)
                elif self.loop == "queue":
                    self.queue.append(track)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            await self.destroy()

    async def destroy(self, reason: Optional[str] = None):
        if self.closed:
            return
        self.closed = True
        self.queue.clear()
        self.current = None
        self.cancel_empty_timer()
        self.cog.players.pop(self.guild.id, None)
        vc = self.guild.voice_client
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        if self.task is not asyncio.current_task():
            self.task.cancel()
        if reason:
            await self.say(reason)

    # ---- leave when everyone else has left ----
    def humans_present(self) -> bool:
        vc = self.guild.voice_client
        return bool(vc and vc.channel and any(not m.bot for m in vc.channel.members))

    def check_empty(self):
        if self.guild.voice_client is None:
            return
        if self.humans_present():
            self.cancel_empty_timer()
        elif self.empty_task is None:
            self.empty_task = asyncio.create_task(self._empty_wait())

    async def _empty_wait(self):
        try:
            await asyncio.sleep(EMPTY_TIMEOUT)
        except asyncio.CancelledError:
            return
        self.empty_task = None
        if not self.humans_present():
            await self.destroy("👋 Left the voice channel because everyone left.")

    def cancel_empty_timer(self):
        if self.empty_task is not None and self.empty_task is not asyncio.current_task():
            self.empty_task.cancel()
        self.empty_task = None


# ---- the cog -----------------------------------------------------------------------
class Music(BaseCog, name="Music"):
    """Play songs in a voice channel"""

    help_text = (
        "# Music\n\n"
        "Join a voice channel first, then:\n\n"
        "`{p}play <name|link>` - Play a song or playlist, or add it to the queue (alias `p`)\n"
        "`{p}play` + an attached audio file - Play an uploaded file (mp3, wav, ogg, flac, m4a ...)\n"
        "`{p}play` as a reply to a message with an audio file - Play that file\n"
        "`{p}play <link to a .mp3/.wav/... file>` - Play a direct audio link\n"
        "`{p}pause` / `{p}resume` - Pause or resume\n"
        "`{p}skip` - Skip the current song (alias `s`)\n"
        "`{p}stop` - Stop and clear the queue\n"
        "`{p}queue` - Show the queue (alias `q`)\n"
        "`{p}nowplaying` - Show the current song (alias `np`)\n"
        "`{p}loop [off|track|queue]` - Repeat the song or the queue\n"
        "`{p}shuffle` - Shuffle the queue\n"
        "`{p}remove <position>` - Remove a song from the queue (alias `rm`)\n"
        "`{p}clear` - Empty the queue\n"
        "`{p}volume [0-100]` - Show or set the volume (alias `vol`)\n"
        "`{p}join` / `{p}leave` - Make me join or leave your voice channel"
    )

    def __init__(self, bot):
        self.bot = bot
        self.players: dict = {}
        if not ensure_opus():
            print("[music] Opus library not found, the bot will join voice but stay silent. "
                  "Install libopus on the host (or ask the host to).")

    async def cog_unload(self):
        for player in list(self.players.values()):
            await player.destroy()

    # ---- helpers ------------------------------------------------------------
    async def ready_check(self, ctx, need_ytdlp: bool = True) -> bool:
        needed = (("`yt-dlp`", yt_dlp), ("`PyNaCl`", nacl)) if need_ytdlp else (("`PyNaCl`", nacl),)
        if any(m is None for _, m in needed):
            missing = " and ".join(n for n, m in needed if m is None)
            await ctx.send(f"Music isn't set up yet: {missing} isn't installed (see requirements.txt).")
            return False
        if shutil.which("ffmpeg") is None:
            await ctx.send("Music isn't set up yet: FFmpeg isn't installed on the machine I run on.")
            return False
        return True

    @staticmethod
    def author_channel(ctx):
        return ctx.author.voice.channel if ctx.author.voice else None

    @staticmethod
    def file_track(url: str, title: str, requester: str, page_url: Optional[str] = None) -> Track:
        return Track(title=title, page_url=page_url or url, requester=requester,
                     stream_url=url, resolved_at=time.time(), direct=True)

    async def message_files(self, message: Optional[discord.Message], requester: str) -> list:
        """Tracks for the audio files attached to a message."""
        if message is None:
            return []
        atts = [a for a in message.attachments if attachment_is_audio(a)][:MAX_PLAYLIST]
        tracks = [self.file_track(a.url, file_title(a.filename), requester, message.jump_url) for a in atts]
        durations = await asyncio.gather(*(probe_duration(t.stream_url) for t in tracks))
        for track, seconds in zip(tracks, durations):
            track.duration = seconds
        return tracks

    async def search(self, query: str, requester: str) -> list:
        q = query.strip()
        spotify = SPOTIFY_LINK.match(q) if is_url(q) else None
        apple = APPLE_LINK.match(q) if is_url(q) else None
        if spotify or apple:
            site = "Spotify" if spotify else "Apple Music"
            pairs = await (spotify_pairs(spotify) if spotify else apple_pairs(q))
            if not pairs:
                raise RuntimeError(f"I couldn't read that {site} link. Try the song's name instead.")
            if len(pairs) == 1:
                q = f"{pairs[0][0]} - {pairs[0][1]}".strip(" -")  # one song: search for it right away
            else:
                # several songs: each one is searched on YouTube when its turn comes
                return [
                    Track(title=f"{a} - {t}".strip(" -"), page_url=f"ytsearch1:{a} - {t}".strip(" -"),
                          requester=requester)
                    for a, t in pairs[:MAX_PLAYLIST]
                ]
        if is_audio_url(q):
            if not is_public_url(q):
                raise RuntimeError("I can only play files from public links.")
            track = self.file_track(q, file_title(q), requester)
            track.duration = await probe_duration(q)
            return [track]
        playlist = is_url(q) and wants_playlist(q)
        info = await asyncio.to_thread(extract, q if is_url(q) else f"ytsearch1:{q}", playlist)
        if not info:
            return []
        if "entries" in info:
            entries = [e for e in (info["entries"] or []) if e]
            if not playlist:
                entries = entries[:1]
        else:
            entries = [info]
        tracks = [track_from_info(e, requester) for e in entries]
        return [t for t in tracks if t][:MAX_PLAYLIST]

    async def resolve(self, track: Track):
        """Make sure the track has a fresh audio link."""
        if track.direct:
            return
        if track.stream_url and time.time() - track.resolved_at < URL_TTL:
            return
        info = await asyncio.to_thread(extract, track.page_url, False)
        if info and "entries" in info:
            info = next((e for e in info["entries"] if e), None)
        if not info or not info.get("url"):
            raise RuntimeError("couldn't get an audio stream for it")
        track.stream_url = info["url"]
        track.resolved_at = time.time()
        if track.page_url.startswith("ytsearch") and info.get("webpage_url"):
            track.page_url = info["webpage_url"]
        track.duration = track.duration if track.duration is not None else info.get("duration")
        track.thumbnail = track.thumbnail or info.get("thumbnail")

    async def ensure_voice(self, ctx, channel):
        """Connect (or move) to the author's channel. Returns the voice client, or None after replying."""
        recorder = self.bot.get_cog("Recorder")
        if recorder is not None and recorder.is_recording(ctx.guild):
            await ctx.send("I'm recording a call right now. Use `record stop` first.")
            return None
        me = ctx.guild.me
        perms = channel.permissions_for(me)
        if not (perms.connect and perms.speak):
            await ctx.send(f"I need Connect and Speak permissions in {channel.mention}.")
            return None
        vc = ctx.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect(self_deaf=True)
            elif vc.channel != channel:
                player = self.players.get(ctx.guild.id)
                if vc.is_playing() or vc.is_paused() or (player and player.queue):
                    await ctx.send(f"I'm already playing in {vc.channel.mention}.")
                    return None
                await vc.move_to(channel)
        except Exception as e:
            await ctx.send(f"❌ I couldn't join the voice channel: {clean_error(e)}")
            return None
        return vc

    def player_for(self, ctx) -> GuildPlayer:
        player = self.players.get(ctx.guild.id)
        if player is None or player.closed:
            player = GuildPlayer(self, ctx.guild, ctx.channel)
            self.players[ctx.guild.id] = player
        else:
            player.channel = ctx.channel
        return player

    async def control(self, ctx) -> Optional[GuildPlayer]:
        """The player, if the bot is in a voice channel and the author is in it too."""
        player, vc = self.players.get(ctx.guild.id), ctx.guild.voice_client
        if player is None or vc is None:
            await ctx.send("I'm not playing anything.")
            return None
        if self.author_channel(ctx) != vc.channel:
            await ctx.send(f"Join {vc.channel.mention} to control the music.")
            return None
        return player

    # ---- commands ---------------------------------------------------------------
    @commands.command(aliases=["p"])
    async def play(self, ctx, *, query: Optional[str] = None):
        """Play a song, playlist or audio file (attach one, reply to one, or link a .mp3/.wav/...)"""
        # audio files: attached to this message, or attached to the message being replied to
        ref = ctx.message.reference.resolved if ctx.message.reference else None
        sources = [ctx.message]
        if not query and isinstance(ref, discord.Message):  # a reply with no song name plays the replied-to file
            sources.append(ref)
        has_files = any(attachment_is_audio(a) for m in sources for a in m.attachments)
        if not query and not has_files:
            return await ctx.send(
                f"Usage: `{ctx.clean_prefix}play <name|link>`, or attach an audio file, "
                "or reply to a message with one."
            )
        direct_link = bool(query) and is_audio_url(query.strip())
        if not await self.ready_check(ctx, need_ytdlp=bool(query) and not direct_link):
            return
        if shutil.which("ffmpeg") is None:
            return await ctx.send("Music isn't set up yet: FFmpeg isn't installed on the machine I run on.")
        channel = self.author_channel(ctx)
        if channel is None:
            return await ctx.send("Join a voice channel first.")
        async with ctx.typing():
            tracks = []
            for msg in sources:
                found = await self.message_files(msg, ctx.author.display_name)
                tracks.extend(found)
                if found:
                    break  # files on the command itself win over the replied-to message
            if query:
                try:
                    tracks.extend(await self.search(query, ctx.author.display_name))
                except Exception as e:
                    if not tracks:
                        return await ctx.send(f"❌ Couldn't load that: {clean_error(e)}")
        if not tracks:
            return await ctx.send("I couldn't find anything for that.")
        if await self.ensure_voice(ctx, channel) is None:
            return
        player = self.player_for(ctx)
        room = MAX_QUEUE - len(player.queue)
        if room <= 0:
            return await ctx.send(f"The queue is full ({MAX_QUEUE} songs).")
        tracks = tracks[:room]
        busy = player.current is not None or bool(player.queue)
        position = len(player.queue) + 1
        player.queue.extend(tracks)
        player.wake.set()
        if len(tracks) > 1:
            await ctx.send(f"➕ Queued **{len(tracks)}** songs.")
        elif busy:
            await ctx.send(f"➕ Added **{esc(tracks[0].title)}** to the queue (#{position}).")

    @commands.command()
    async def pause(self, ctx):
        """Pause the music"""
        if not await self.control(ctx):
            return
        vc = ctx.guild.voice_client
        if not vc.is_playing():
            return await ctx.send("Nothing is playing.")
        vc.pause()
        await ctx.send("⏸️ Paused.")

    @commands.command()
    async def resume(self, ctx):
        """Resume the music"""
        if not await self.control(ctx):
            return
        vc = ctx.guild.voice_client
        if not vc.is_paused():
            return await ctx.send("Nothing is paused.")
        vc.resume()
        await ctx.send("▶️ Resumed.")

    @commands.command(aliases=["s", "next"])
    async def skip(self, ctx):
        """Skip the current song"""
        player = await self.control(ctx)
        if not player:
            return
        vc = ctx.guild.voice_client
        if not (vc.is_playing() or vc.is_paused()):
            return await ctx.send("Nothing is playing.")
        title = player.current.title if player.current else "the song"
        player.skipped = True
        vc.stop()
        await ctx.send(f"⏭️ Skipped **{esc(title)}**.")

    @commands.command()
    async def stop(self, ctx):
        """Stop the music and clear the queue"""
        player = await self.control(ctx)
        if not player:
            return
        vc = ctx.guild.voice_client
        player.queue.clear()
        player.loop = "off"
        if vc.is_playing() or vc.is_paused():
            player.skipped = True
            vc.stop()
        await ctx.send("⏹️ Stopped and cleared the queue.")

    @commands.command(aliases=["q"])
    async def queue(self, ctx):
        """Show the queue"""
        player = self.players.get(ctx.guild.id)
        if player is None or (player.current is None and not player.queue):
            return await ctx.send("The queue is empty.")
        lines = []
        if player.current:
            lines.append(f"🎶 **Now playing:** {esc(player.current.title)} `{player.current.length}`")
        for i, t in enumerate(player.queue, 1):
            lines.append(f"`{i}.` {esc(t.title)} `{t.length}` — {esc(t.requester)}")
        title = f"Queue ({len(player.queue)})" + ("" if player.loop == "off" else f" · loop: {player.loop}")
        await paginate(ctx, chunk_pages(title, lines, COLOR, per_page=10))

    @commands.command(aliases=["np", "current"])
    async def nowplaying(self, ctx):
        """Show the current song"""
        player = self.players.get(ctx.guild.id)
        vc = ctx.guild.voice_client
        if player is None or player.current is None or vc is None:
            return await ctx.send("Nothing is playing.")
        title = "Paused" if vc.is_paused() else "Now playing"
        embed = player.now_playing_embed(player.current, title)
        embed.set_footer(text=f"Volume {int(player.volume * 100)}% · Loop: {player.loop}")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @commands.command(aliases=["vol"])
    async def volume(self, ctx, level: Optional[int] = None):
        """Show or set the volume (0-100)"""
        player = self.players.get(ctx.guild.id)
        if level is None:
            return await ctx.send(f"🔊 Volume is {int(player.volume * 100) if player else 50}%.")
        player = await self.control(ctx)
        if not player:
            return
        if not 0 <= level <= 100:
            return await ctx.send("Pick a volume between 0 and 100.")
        player.volume = level / 100
        source = ctx.guild.voice_client.source
        if source is not None and hasattr(source, "volume"):
            source.volume = player.volume
        await ctx.send(f"🔊 Volume set to {level}%.")

    @commands.command(aliases=["repeat"])
    async def loop(self, ctx, mode: Optional[str] = None):
        """Repeat the song or the whole queue (off, track, queue)"""
        player = await self.control(ctx)
        if not player:
            return
        if mode is None:
            new = {"off": "track", "track": "queue", "queue": "off"}[player.loop]
        elif mode.lower() in LOOP_MODES:
            new = LOOP_MODES[mode.lower()]
        else:
            return await ctx.send(f"Usage: `{ctx.clean_prefix}loop [off|track|queue]`")
        player.loop = new
        await ctx.send(f"🔁 Loop is now **{new}**.")

    @commands.command()
    async def shuffle(self, ctx):
        """Shuffle the queue"""
        player = await self.control(ctx)
        if not player:
            return
        if len(player.queue) < 2:
            return await ctx.send("There's nothing to shuffle.")
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await ctx.send(f"🔀 Shuffled {len(items)} songs.")

    @commands.command(aliases=["rm"])
    async def remove(self, ctx, position: int):
        """Remove a song from the queue by its number"""
        player = await self.control(ctx)
        if not player:
            return
        if not 1 <= position <= len(player.queue):
            return await ctx.send(f"Pick a number between 1 and {len(player.queue)}." if player.queue
                                  else "The queue is empty.")
        items = list(player.queue)
        gone = items.pop(position - 1)
        player.queue = deque(items)
        await ctx.send(f"🗑️ Removed **{esc(gone.title)}**.")

    @commands.command()
    async def clear(self, ctx):
        """Empty the queue (the current song keeps playing)"""
        player = await self.control(ctx)
        if not player:
            return
        count = len(player.queue)
        player.queue.clear()
        await ctx.send(f"🧹 Cleared {count} song(s).")

    @staticmethod
    async def tick(ctx):
        """React with a checkmark on the command message instead of sending a reply."""
        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            await ctx.send("✅")  # no permission to add reactions, so fall back to a message

    @commands.command(aliases=["summon"])
    async def join(self, ctx):
        """Make me join your voice channel"""
        if not await self.ready_check(ctx):
            return
        channel = self.author_channel(ctx)
        if channel is None:
            return await ctx.send("Join a voice channel first.")
        if await self.ensure_voice(ctx, channel) is None:
            return
        self.player_for(ctx)
        await self.tick(ctx)

    @commands.command(aliases=["dc"])
    async def leave(self, ctx):
        """Make me leave the voice channel"""
        vc = ctx.guild.voice_client
        if vc is None:
            return await ctx.send("I'm not in a voice channel.")
        recorder = self.bot.get_cog("Recorder")
        if recorder is not None and recorder.is_recording(ctx.guild):
            return await ctx.send("I'm recording a call right now. Use `record stop` first.")
        if self.author_channel(ctx) != vc.channel:
            return await ctx.send(f"Join {vc.channel.mention} to make me leave.")
        player = self.players.get(ctx.guild.id)
        if player:
            await player.destroy()
        else:
            await vc.disconnect(force=True)
        await self.tick(ctx)

    # ---- voice events -----------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        player = self.players.get(member.guild.id)
        if player is None:
            return
        if member.id == self.bot.user.id:
            if after.channel is None:  # kicked or disconnected
                await player.destroy()
            else:
                player.check_empty()
            return
        vc = member.guild.voice_client
        if vc and vc.channel and vc.channel in (before.channel, after.channel):
            player.check_empty()


async def setup(bot):
    add_cog_safely(bot, Music(bot))