import asyncio
import os
import pathlib
import sys

# Always run from this file's folder so cogs/ and data/ are found
# no matter where the host starts the bot from.
HERE = pathlib.Path(__file__).resolve().parent
os.chdir(HERE)

# The host installs packages with `pip --prefix .local`, which Python does not
# search by default. Add every matching .local site-packages folder ourselves.
_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
for _base in (HERE, HERE.parent, pathlib.Path("/home/container")):
    _sp = _base / ".local" / "lib" / _ver / "site-packages"
    if _sp.is_dir() and str(_sp) not in sys.path:
        sys.path.insert(0, str(_sp))

import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils.store import GuildStore

load_dotenv(HERE / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "-")

# All three privileged intents below must also be switched on in the Developer Portal
# (Bot > Privileged Gateway Intents), or the bot refuses to connect.
intents = discord.Intents.default()
intents.message_content = True  # needed for prefix commands
intents.members = True  # needed for role limits
intents.presences = True  # needed for Vanity (reads custom statuses)


def get_prefix(bot, message):
    """Per-server prefix, falling back to the default. Mentioning the bot always works."""
    return commands.when_mentioned_or(bot.guild_prefix(message.guild))(bot, message)


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=None,  # custom help lives in cogs/general.py
            case_insensitive=True,
            status=discord.Status.dnd,
        )
        self.bot_name = os.getenv("BOT_NAME", "heal")
        self.prefix_display = PREFIX  # the default prefix
        # saved to data/prefixes.json, so each server keeps its prefix across restarts
        self.prefixes = GuildStore("prefixes", {"prefix": None})
        self.credit = os.getenv("CODED_BY", "")

    def guild_prefix(self, guild) -> str:
        if guild is None:
            return self.prefix_display
        return self.prefixes.get(guild.id).get("prefix") or self.prefix_display

    async def setup_hook(self):
        for path in sorted(pathlib.Path("cogs").glob("*.py")):
            if path.stem.startswith("_"):
                continue
            try:
                await self.load_extension(f"cogs.{path.stem}")
                print(f"loaded cogs.{path.stem}")
            except Exception as e:
                print(f"FAILED cogs.{path.stem}: {e!r}")

    async def on_message(self, message):
        # Set DEBUG=1 in .env to see every message the bot receives. If the text shows up
        # empty (''), the Message Content intent is switched off in the Developer Portal.
        if os.getenv("DEBUG") and message.guild and not message.author.bot:
            print(f"[msg] #{message.channel} {message.author}: {message.content!r}")
        await self.process_commands(message)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if ctx.command and ctx.command.has_error_handler():
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return
        error = getattr(error, "original", error)
        print(f"[error] {ctx.command}: {error!r}")

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")


async def main():
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file")
    discord.utils.setup_logging()  # shows connection/gateway problems in the console
    async with Bot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())