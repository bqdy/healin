import discord
from discord import ui
from discord.ext import commands

# Order matches the overview page. Names must match each cog's `name=`.
CATEGORIES = [
    "AntiNuke",
    "Role Limits",
    "Roles",
    "Vanity",
    "Aliases",
    "Server Configuration",
    "Backup",
    "Live Channel Counters",
    "Activity Roles",
    "Event Logging",
    "Force Management",
    "Vouch",
    "Giveaway",
    "Moderation",
    "Tickets",
    "Disconnect Security",
    "VoiceMaster",
    "VC Commands",
    "VC Utility",
    "Recorder",
    "Rank Management",
    "Community Utilities",
    "Utility",
    "Fun",
    "Economy",
    "General",
]


def overview_text(bot: commands.Bot, prefix: str) -> str:
    header = f"# Bot Overview\n\n**Bot:** {bot.bot_name}\n**Prefix:** {prefix}"
    if bot.credit:
        header += f"\n**Created by:** {bot.credit}"

    lines = []
    for name in CATEGORIES:
        cog = bot.get_cog(name)
        desc = (cog.description or "").strip() if cog else ""
        lines.append(f"**{name}** - {desc}" if desc else f"**{name}**")

    return f"{header}\n\n\n## Categories\n\n" + "\n".join(lines)


def category_text(bot: commands.Bot, name: str, prefix: str) -> str:
    cog = bot.get_cog(name)
    if cog is None:
        return f"# {name}\n\nThis category isn't loaded."

    # a cog can ship its own hand-written help page (see cogs/antinuke.py)
    custom = getattr(cog, "help_text", None)
    if custom:
        if not isinstance(custom, str):  # a list of pages: this returns the first one
            custom = custom[0]
        return custom.replace("{p}", prefix)[:3900]

    text = f"# {name}\n\n{(cog.description or '').strip()}\n\n## Commands\n\n"
    cmds = sorted(cog.get_commands(), key=lambda c: c.name)
    if not cmds:
        return text + "No commands yet."

    lines = [
        f"`{prefix}{c.qualified_name}` - {c.short_doc or 'No description'}"
        for c in cmds
    ]
    return (text + "\n".join(lines))[:3800]  # components allow ~4000 chars of text


def category_pages(bot: commands.Bot, name: str, prefix: str) -> list:
    """Every page of a category. A cog can set help_text to a list to get Previous / Next buttons."""
    cog = bot.get_cog(name)
    custom = getattr(cog, "help_text", None) if cog else None
    if custom and not isinstance(custom, str):
        return [page.replace("{p}", prefix)[:3800] for page in custom]
    return [category_text(bot, name, prefix)]


class HelpSelect(ui.Select):
    def __init__(self, layout: "HelpLayout", current: str):
        # Discord allows at most 25 options in a dropdown, so Overview is a button instead
        # (see HelpLayout.render). If CATEGORIES ever grows past 25, the extras need a second page.
        options = [
            discord.SelectOption(label=n, value=n, default=current == n) for n in CATEGORIES[:25]
        ]
        super().__init__(placeholder="Choose a category", options=options)
        self.layout = layout

    async def callback(self, interaction: discord.Interaction):
        self.layout.render(self.values[0])
        await interaction.response.edit_message(view=self.layout)


class HelpLayout(ui.LayoutView):
    """Help menu built from Components V2 (needs discord.py 2.6+)."""

    def __init__(self, bot: commands.Bot, author: discord.abc.User, prefix: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.prefix = prefix
        self.author = author
        self.message: discord.Message | None = None
        self.select: HelpSelect | None = None
        self.page = "Overview"
        self.page_no = 0
        self.render("Overview")

    def render(self, page: str, page_no: int = 0):
        self.clear_items()
        pages = None
        if page == "Overview":
            text = overview_text(self.bot, self.prefix)
        else:
            pages = category_pages(self.bot, page, self.prefix)
            page_no = max(0, min(page_no, len(pages) - 1))
            text = pages[page_no]
            if len(pages) > 1:
                text += f"\n\n-# Page {page_no + 1}/{len(pages)}"
        self.page = page
        self.page_no = page_no if pages else 0

        container = ui.Container()
        container.add_item(ui.TextDisplay(text))
        container.add_item(ui.Separator())

        nav = ui.ActionRow()
        home_btn = ui.Button(
            label="Overview", style=discord.ButtonStyle.primary, disabled=page == "Overview"
        )
        home_btn.callback = self.go_home
        nav.add_item(home_btn)
        if pages and len(pages) > 1:
            prev_btn = ui.Button(
                label="Previous", style=discord.ButtonStyle.secondary, disabled=page_no == 0
            )
            next_btn = ui.Button(
                label="Next", style=discord.ButtonStyle.secondary, disabled=page_no >= len(pages) - 1
            )
            prev_btn.callback = self.go_previous
            next_btn.callback = self.go_next
            nav.add_item(prev_btn)
            nav.add_item(next_btn)
        container.add_item(nav)

        self.select = HelpSelect(self, page)
        row = ui.ActionRow()
        row.add_item(self.select)
        container.add_item(row)

        self.add_item(container)

    async def go_home(self, interaction: discord.Interaction):
        self.render("Overview")
        await interaction.response.edit_message(view=self)

    async def go_previous(self, interaction: discord.Interaction):
        self.render(self.page, self.page_no - 1)
        await interaction.response.edit_message(view=self)

    async def go_next(self, interaction: discord.Interaction):
        self.render(self.page, self.page_no + 1)
        await interaction.response.edit_message(view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This menu isn't yours. Run the help command yourself.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        if self.select:
            self.select.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class General(commands.Cog, name="General"):
    """Help shortcuts and bot latency"""

    # hand-written help page for the General category ({p} becomes the server's prefix)
    help_text = (
        "# General\n\n"
        "Help shortcuts, bot latency and the server prefix.\n\n"
        "## Commands\n\n"
        "`{p}help [category]` - Show the help menu, or a single category.\n"
        "`{p}ping` - Show the bot's latency.\n"
        "`{p}prefix` - Show this server's prefix.\n"
        "`{p}prefix <new prefix>` - Change this server's prefix (needs Manage Server).\n"
        "`{p}prefix reset` - Go back to the default prefix.\n\n"
        "## Server whitelist (bot owner and `uwl` users)\n\n"
        "`{p}swl <server id> <time>` - Whitelist a server for a while (e.g. `7 days`, `12h`, `1 year`, `yearly`, or `lifetime`).\n"
        "`{p}swl remove <server id>` - Remove a server's whitelist and leave it.\n"
        "`{p}swl list` - Show whitelisted servers.\n"
        "The bot leaves any server that isn't whitelisted, or whose whitelist runs out. Servers it's already in get a lifetime whitelist automatically.\n\n"
        "## Music (join a voice channel first)\n\n"
        "`{p}play <name|link>` - Play a song or playlist from a name, YouTube, SoundCloud, Spotify or Apple Music link, or queue it (alias `p`)\n"
        "`{p}pause` / `{p}resume` - Pause or resume\n"
        "`{p}skip` - Skip the song (alias `s`)\n"
        "`{p}stop` - Stop and clear the queue\n"
        "`{p}queue` - Show the queue (alias `q`)\n"
        "`{p}nowplaying` - Show the current song (alias `np`)\n"
        "`{p}loop [off|track|queue]` - Repeat the song or queue\n"
        "`{p}shuffle` - Shuffle the queue\n"
        "`{p}remove <position>` - Remove a song from the queue (alias `rm`)\n"
        "`{p}clear` - Empty the queue\n"
        "`{p}volume [0-100]` - Show or set the volume (alias `vol`)\n"
        "`{p}join` / `{p}leave` - Make me join or leave your voice channel (alias `dc`)\n\n"
        "## User whitelist\n\n"
        "`{p}uwl add <user>` - Let someone use the `swl` commands (bot owner only).\n"
        "`{p}uwl remove <user>` - Take their access away (bot owner only).\n"
        "`{p}uwl list` - Show who has access.\n"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="help", aliases=["h", "commands"])
    async def help_command(self, ctx: commands.Context, *, category: str | None = None):
        """Show the help menu, or a single category."""
        view = HelpLayout(self.bot, ctx.author, self.bot.guild_prefix(ctx.guild))
        if category:
            match = next((n for n in CATEGORIES if n.lower() == category.lower()), None)
            if match:
                view.render(match)
        view.message = await ctx.send(view=view)

    @commands.command(name="prefix", aliases=["setprefix"])
    @commands.guild_only()
    async def prefix_command(self, ctx: commands.Context, new: str | None = None):
        """Show or change this server's prefix. Use `reset` to go back to the default."""
        current = self.bot.guild_prefix(ctx.guild)
        if new is None:
            return await ctx.send(
                f"This server's prefix is `{current}`. "
                f"Change it with `{current}prefix <new prefix>` (needs Manage Server). "
                "Mentioning me always works too."
            )

        perms = ctx.author.guild_permissions
        if not (perms.manage_guild or perms.administrator or ctx.author.id == ctx.guild.owner_id):
            return await ctx.send("You need the **Manage Server** permission to change the prefix.")

        cfg = self.bot.prefixes.get(ctx.guild.id)
        if new.lower() == "reset":
            cfg["prefix"] = None
            self.bot.prefixes.save()
            return await ctx.send(f"Prefix reset to the default: `{self.bot.prefix_display}`")

        if len(new) > 5 or any(ch in new for ch in "`@<>"):
            return await ctx.send(
                "A prefix can be up to 5 characters, with no spaces, backticks, `@` or `<` `>`."
            )

        cfg["prefix"] = new
        self.bot.prefixes.save()
        await ctx.send(f"Prefix for this server is now `{new}`. It stays saved even if I restart.")

    @commands.command(name="ping", aliases=["latency"])
    async def ping(self, ctx: commands.Context):
        """Show the bot's latency."""
        await ctx.send(f"Pong! `{round(self.bot.latency * 1000)}ms`")


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))