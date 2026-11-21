import copy
import json
import pathlib
import re

import discord
from discord.ext import commands

STORE_PATH = pathlib.Path("data/aliases_store.json")
PLACEHOLDER_RE = re.compile(r"\{(\d+)\}|\{args\}")
SHORTCUT_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def load_store():
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text())
    return {}


def save_store(store):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(store, indent=2))


def first_word(text):
    return text.split(maxsplit=1)[0].lower() if text.strip() else ""


class Aliases(commands.Cog, name="Aliases"):
    """Create custom shortcuts that run other commands"""

    # general.py shows this page as-is; {p} becomes the bot's prefix
    help_text = (
        "# Aliases\n\n"
        "**Required:** Manage Server\n\n"
        "`{p}alias add <shortcut>, <command>` - Create a shortcut that runs a command\n"
        "Example: `{p}alias add b, ban` lets staff use `{p}b @user reason` as `{p}ban @user reason`.\n"
        "`{p}alias add bando, r {0} <role ID>` - Use the first argument in a shortcut\n"
        "Placeholders: `{0}` first argument, `{1}` second argument, `{args}` all arguments.\n"
        "`{p}alias remove <shortcut>` - Delete one shortcut\n"
        "`{p}alias removeall <command>` - Delete every shortcut for a command\n"
        "`{p}alias view <shortcut>` - View the command behind a shortcut\n"
        "`{p}alias list` - List all shortcuts\n"
        "`{p}alias reset` - Remove every shortcut"
    )

    def __init__(self, bot):
        self.bot = bot
        self._orig_process = None

    # Hook into command processing so shortcuts run as the real command

    async def cog_load(self):
        self._orig_process = self.bot.process_commands
        self.bot.process_commands = self._process_commands

    async def cog_unload(self):
        if self._orig_process:
            self.bot.process_commands = self._orig_process

    async def _process_commands(self, message):
        try:
            rewritten = await self.rewrite(message)
        except Exception:
            rewritten = None
        if rewritten is not None:
            fake = copy.copy(message)
            fake.content = rewritten
            message = fake
        await self._orig_process(message)

    async def rewrite(self, message):
        if message.guild is None or message.author.bot or not message.content:
            return None
        guild_aliases = load_store().get(str(message.guild.id))
        if not guild_aliases:
            return None
        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        prefix = next((p for p in prefixes if message.content.startswith(p)), None)
        if prefix is None:
            return None

        body = message.content[len(prefix):].lstrip()
        name, _, rest = body.partition(" ")
        name = name.lower()
        # real commands always win over shortcuts
        if self.bot.get_command(name) or name not in guild_aliases:
            return None

        rest = rest.strip()
        args = rest.split()
        template = guild_aliases[name]
        if PLACEHOLDER_RE.search(template):
            def fill(m):
                if m.group(1) is None:
                    return rest
                i = int(m.group(1))
                return args[i] if i < len(args) else ""
            command = PLACEHOLDER_RE.sub(fill, template)
        else:
            command = f"{template} {rest}".strip()
        return prefix + command

    def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.MissingPermissions(["manage_guild"])
        return True

    # -alias

    @commands.group(name="alias", invoke_without_command=True, help="Manage command shortcuts")
    async def alias(self, ctx):
        p = ctx.clean_prefix
        await ctx.send(
            f"`{p}alias add <shortcut>, <command>`, `{p}alias remove <shortcut>`, "
            f"`{p}alias removeall <command>`, `{p}alias view <shortcut>`, "
            f"`{p}alias list`, `{p}alias reset`"
        )

    @alias.command(name="add", help="Create a shortcut that runs a command")
    async def alias_add(self, ctx, *, text: str):
        shortcut, sep, command = text.partition(",")
        shortcut = shortcut.strip().lower()
        command = command.strip()
        prefix = getattr(self.bot, "prefix_display", "-")
        if command.startswith(prefix):
            command = command[len(prefix):].strip()
        if not sep or not shortcut or not command:
            return await ctx.send(f"Usage: `{ctx.clean_prefix}alias add <shortcut>, <command>`")
        if not SHORTCUT_RE.match(shortcut):
            return await ctx.send("A shortcut must be one word (letters, numbers, underscores).")
        if self.bot.get_command(shortcut):
            return await ctx.send(f"`{shortcut}` is already a real command, so it can't be a shortcut.")
        if not self.bot.get_command(first_word(command)):
            return await ctx.send(f"`{first_word(command)}` isn't a command I know.")

        store = load_store()
        guild_aliases = store.setdefault(str(ctx.guild.id), {})
        updated = shortcut in guild_aliases
        guild_aliases[shortcut] = command
        save_store(store)
        verb = "Updated" if updated else "Created"
        await ctx.send(
            f"{verb} shortcut `{ctx.clean_prefix}{shortcut}` -> `{ctx.clean_prefix}{command}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @alias.command(name="remove", help="Delete one shortcut")
    async def alias_remove(self, ctx, shortcut: str):
        store = load_store()
        guild_aliases = store.get(str(ctx.guild.id), {})
        if guild_aliases.pop(shortcut.lower(), None) is None:
            return await ctx.send("That shortcut doesn't exist.")
        save_store(store)
        await ctx.send(f"Deleted shortcut `{shortcut.lower()}`.")

    @alias.command(name="removeall", help="Delete every shortcut for a command")
    async def alias_removeall(self, ctx, *, command: str):
        target = first_word(command.removeprefix(getattr(self.bot, "prefix_display", "-")))
        store = load_store()
        guild_aliases = store.get(str(ctx.guild.id), {})
        gone = [s for s, c in guild_aliases.items() if first_word(c) == target]
        if not gone:
            return await ctx.send(f"No shortcuts point to `{target}`.")
        for s in gone:
            del guild_aliases[s]
        save_store(store)
        await ctx.send(f"Deleted {len(gone)} shortcut(s) for `{target}`.")

    @alias.command(name="view", help="View the command behind a shortcut")
    async def alias_view(self, ctx, shortcut: str):
        command = load_store().get(str(ctx.guild.id), {}).get(shortcut.lower())
        if not command:
            return await ctx.send("That shortcut doesn't exist.")
        p = ctx.clean_prefix
        await ctx.send(
            f"`{p}{shortcut.lower()}` runs `{p}{command}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @alias.command(name="list", help="List all shortcuts")
    async def alias_list(self, ctx):
        guild_aliases = load_store().get(str(ctx.guild.id), {})
        if not guild_aliases:
            return await ctx.send("No shortcuts yet.")
        p = ctx.clean_prefix
        lines = [f"`{p}{s}` -> `{p}{c}`" for s, c in sorted(guild_aliases.items())]
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 1900:
                await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    @alias.command(name="reset", help="Remove every shortcut")
    async def alias_reset(self, ctx):
        store = load_store()
        count = len(store.pop(str(ctx.guild.id), {}))
        save_store(store)
        await ctx.send(f"Removed {count} shortcut(s).")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Manage Server to do that.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in a server.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Aliases(bot))
