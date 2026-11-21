import asyncio
import traceback


def add_cog_safely(bot, cog):
    """Register a cog once every other cog has loaded, skipping any command name or alias that
    another cog already owns (instead of failing to load). Same idea as cogs/moderation.py."""

    async def register():
        try:
            await bot.wait_until_ready()
            taken = bot.all_commands

            if any(c.name in taken for c in cog.get_commands()):
                cog.__cog_commands__ = tuple(c for c in cog.__cog_commands__ if c.name not in taken)

            for cmd in cog.get_commands():
                cmd.aliases = [a for a in cmd.aliases if a not in taken]

            await bot.add_cog(cog)
        except Exception:
            traceback.print_exc()

    task = asyncio.create_task(register())
    if not hasattr(bot, "_safe_cog_tasks"):
        bot._safe_cog_tasks = []
    bot._safe_cog_tasks.append(task)  # keeps a reference so the task isn't garbage collected
    return task
