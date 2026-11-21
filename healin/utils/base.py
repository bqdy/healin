"""Shared pieces for the Utility / Fun / Economy cogs."""
import sys
import traceback

import discord
from discord.ext import commands

COLOR = 0x0F0F10
NO_PINGS = discord.AllowedMentions.none()


class BaseCog(commands.Cog):
    """Server-only cog that answers command errors in plain words (the bot's global handler is silent)."""

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NotOwner):
            msg = "Only the bot owner can use that."
        elif isinstance(error, commands.MissingPermissions):
            msg = "You don't have permission to use that."
        elif isinstance(error, commands.BotMissingPermissions):
            msg = "I'm missing permissions for that."
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                                commands.TooManyArguments, commands.BadUnionArgument,
                                commands.BadLiteralArgument, commands.UserInputError)):
            usage = f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            msg = f"Usage: `{usage}`"
        elif isinstance(error, commands.CommandOnCooldown):
            msg = f"Slow down, try again in {int(error.retry_after) + 1}s."
        elif isinstance(error, commands.NoPrivateMessage):
            msg = "Server only."
        elif isinstance(error, commands.CheckFailure):
            return  # e.g. a command disabled by the moderation gate, which already replied
        elif isinstance(error, discord.Forbidden):
            msg = "I'm missing permissions or my role is too low for that."
        else:
            print(f"Error in {ctx.command}:", file=sys.stderr)
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
            msg = f"Something went wrong: `{error}`"
        await ctx.send(msg, allowed_mentions=NO_PINGS)
