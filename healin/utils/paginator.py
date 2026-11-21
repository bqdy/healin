import discord
from discord import ui


class Paginator(ui.View):
    def __init__(self, author_id: int, pages: list, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.pages = pages
        self.index = 0
        self.message = None
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    def current(self) -> discord.Embed:
        embed = self.pages[self.index]
        embed.set_footer(text=f"Page {self.index + 1}/{len(self.pages)}")
        return embed

    @ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: ui.Button):
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.current(), view=self)

    @ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: ui.Button):
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=self.current(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def chunk_pages(title: str, lines: list, color: int, per_page: int = 15, empty: str = "Nothing here.") -> list:
    """Turn a list of text lines into embed pages."""
    if not lines:
        return [discord.Embed(title=title, description=empty, color=color)]
    pages = []
    for i in range(0, len(lines), per_page):
        pages.append(discord.Embed(title=title, description="\n".join(lines[i:i + per_page])[:4000], color=color))
    return pages


async def paginate(ctx, pages: list):
    if len(pages) == 1:
        return await ctx.send(embed=pages[0], allowed_mentions=discord.AllowedMentions.none())
    view = Paginator(ctx.author.id, pages)
    view.message = await ctx.send(embed=view.current(), view=view,
                                  allowed_mentions=discord.AllowedMentions.none())
    return view.message
