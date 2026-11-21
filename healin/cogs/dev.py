import pathlib

from discord.ext import commands

# The only account that can use these commands. Everyone else gets no reply at all.
BOT_OWNER_ID = 482506940027502597
COGS_DIR = pathlib.Path(__file__).resolve().parent


def owner_only():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.id == BOT_OWNER_ID

    return commands.check(predicate)


class Dev(commands.Cog, name="Dev"):
    """Reload cogs without restarting the bot (owner only, hidden from help)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")
        raise error

    @staticmethod
    def ext_name(name: str) -> str:
        name = name.strip().lower().removesuffix(".py")
        return name if name.startswith("cogs.") else f"cogs.{name}"

    def all_exts(self) -> list:
        return [f"cogs.{p.stem}" for p in sorted(COGS_DIR.glob("*.py")) if not p.stem.startswith("_")]

    @commands.command(name="reload", aliases=["rl"])
    @owner_only()
    async def reload(self, ctx: commands.Context, *names: str):
        """Reload cogs: reload <cog> [cog...] or reload all (loads it if it wasn't loaded)"""
        if not names:
            raise commands.MissingRequiredArgument(ctx.command.clean_params["names"])

        targets = self.all_exts() if names[0].lower() == "all" else [self.ext_name(n) for n in names]
        # vc_commands hangs its commands on the `vc` group that vc_utility owns, so reloading
        # vc_utility wipes them: always reload them together, vc_utility first
        if "cogs.vc_utility" in targets or "cogs.vc_commands" in targets:
            targets = [t for t in targets if t not in ("cogs.vc_utility", "cogs.vc_commands")]
            targets += ["cogs.vc_utility", "cogs.vc_commands"]
        ok, failed = [], []
        for ext in targets:
            if ext == "cogs.dev":
                continue  # don't reload the thing that's running this command
            try:
                if ext in self.bot.extensions:
                    await self.bot.reload_extension(ext)
                else:
                    await self.bot.load_extension(ext)
                ok.append(ext.removeprefix("cogs."))
            except commands.ExtensionNotFound:
                failed.append(f"`{ext.removeprefix('cogs.')}`: no such file")
            except Exception as e:
                # a broken cog stays on its old code, the bot keeps running
                failed.append(f"`{ext.removeprefix('cogs.')}`: {type(getattr(e, 'original', e)).__name__}: {getattr(e, 'original', e)}")

        lines = []
        if ok:
            lines.append(f"Reloaded: {', '.join(f'`{x}`' for x in ok)}")
        lines.extend(f"Failed: {f}" for f in failed)
        await ctx.send("\n".join(lines)[:1900] or "Nothing to reload.")

    @commands.command(name="unload")
    @owner_only()
    async def unload(self, ctx: commands.Context, name: str):
        """Unload a cog"""
        ext = self.ext_name(name)
        if ext == "cogs.dev":
            return await ctx.send("I won't unload the reload tools.")
        try:
            await self.bot.unload_extension(ext)
        except commands.ExtensionNotLoaded:
            return await ctx.send("That cog isn't loaded.")
        await ctx.send(f"Unloaded `{ext.removeprefix('cogs.')}`.")

    @commands.command(name="cogs")
    @owner_only()
    async def cogs(self, ctx: commands.Context):
        """Show which cogs are loaded"""
        loaded = {e for e in self.bot.extensions}
        lines = [f"{'on ' if e in loaded else 'off'} `{e.removeprefix('cogs.')}`" for e in self.all_exts()]
        await ctx.send("\n".join(lines)[:1900])


async def setup(bot: commands.Bot):
    await bot.add_cog(Dev(bot))
