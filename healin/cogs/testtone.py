import asyncio
import shutil

import discord
from discord.ext import commands

def ensure_opus() -> bool:
    if discord.opus.is_loaded():
        return True
    for name in ("libopus.so.0", "libopus.so", "libopus.0.dylib", "opus"):
        try:
            discord.opus.load_opus(name)
            return True
        except Exception:
            continue
    return False


class TestTone(commands.Cog, name="TestTone"):
    """Plays a 5 second beep generated inside the bot, to check voice audio works (hidden from help)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(f"Try again in {int(error.retry_after) + 1}s.")
        raise error

    @staticmethod
    def diagnose(vc, channel) -> str:
        """A few facts about the bot's voice connection, so a silent bot can be traced."""
        me = channel.guild.me
        perms = channel.permissions_for(me)
        state = me.voice
        conn = getattr(vc, "_connection", None)
        dave_ver = getattr(conn, "dave_protocol_version", "n/a")
        dave_sess = getattr(conn, "dave_session", None)
        try:
            import davey
            davey_ver = getattr(davey, "__version__", "?")
        except Exception:
            davey_ver = "NOT INSTALLED"
        lines = [
            f"connected: {vc.is_connected()} | channel: {vc.channel}",
            f"bot can speak here: {perms.speak} | connect: {perms.connect}",
            f"bot server-muted: {getattr(state, 'mute', None)} | suppressed: {getattr(state, 'suppress', None)} | self-muted: {getattr(state, 'self_mute', None)}",
            f"encryption mode: {getattr(vc, 'mode', 'n/a')} | latency: {vc.latency * 1000:.0f} ms",
            f"DAVE protocol: {dave_ver} | DAVE session: {'yes' if dave_sess else 'no'} | davey: {davey_ver}",
            f"DAVE ready: {getattr(dave_sess, 'ready', 'n/a')} | can encrypt: {getattr(conn, 'can_encrypt', 'n/a')} | privacy code: {getattr(vc, 'voice_privacy_code', None)}",
            f"people in call: {len(channel.members)}",
            f"opus loaded: {discord.opus.is_loaded()}",
        ]
        return "```\n" + "\n".join(lines) + "\n```"

    @commands.command(name="testtone", aliases=["beep"])
    @commands.cooldown(1, 15, commands.BucketType.guild)  # stops spamming
    async def testtone(self, ctx: commands.Context):
        """Join your voice channel and play a 5 second beep"""
        if not (ctx.author.voice and ctx.author.voice.channel):
            return await ctx.send("Join a voice channel first.")
        if shutil.which("ffmpeg") is None:
            return await ctx.send("FFmpeg isn't installed on this host.")
        if not ensure_opus():
            return await ctx.send("The Opus library isn't loaded, so the bot can't send audio.")

        vc = ctx.guild.voice_client
        if vc is not None and vc.is_playing():
            return await ctx.send("I'm already playing something here. Stop it first.")

        joined_here = vc is None
        try:
            if vc is None:
                vc = await ctx.author.voice.channel.connect(self_deaf=True)
            elif vc.channel != ctx.author.voice.channel:
                await vc.move_to(ctx.author.voice.channel)
        except Exception as e:
            return await ctx.send(f"Couldn't join the call: `{e}`")

        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        problem = []

        def after(error):
            if error:
                problem.append(error)
            loop.call_soon_threadsafe(done.set)

        await ctx.send(self.diagnose(vc, ctx.author.voice.channel))

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio("sine=frequency=440:duration=5", before_options="-f lavfi", options="-vn"),
            volume=0.4,
        )
        vc.play(source, after=after)
        await ctx.send("Playing a 5 second beep. Do you hear it?")
        try:
            await asyncio.wait_for(done.wait(), timeout=15)
        except asyncio.TimeoutError:
            vc.stop()
        if problem:
            await ctx.send(f"The beep failed: `{problem[0]}`")
        if joined_here:
            await vc.disconnect(force=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TestTone(bot))
