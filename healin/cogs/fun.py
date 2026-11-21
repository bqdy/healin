"""Fun cog: small games and text toys. No outside APIs, so nothing here can go down."""
import hashlib
import random
import re
from typing import Optional

import discord
from discord.ext import commands

from utils.base import COLOR, NO_PINGS, BaseCog
from utils.safe_cog import add_cog_safely

EIGHT_BALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes, definitely.", "You may rely on it.",
    "As I see it, yes.", "Most likely.", "Outlook good.", "Signs point to yes.", "Yes.",
    "Reply hazy, try again.", "Ask again later.", "Better not tell you now.", "Cannot predict now.",
    "Concentrate and ask again.", "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]

WOULD_YOU_RATHER = [
    ("have unlimited money", "have unlimited time"),
    ("always be 10 minutes late", "always be 20 minutes early"),
    ("lose your phone", "lose your wallet"),
    ("only ever eat pizza", "never eat pizza again"),
    ("be able to fly", "be able to turn invisible"),
    ("live without music", "live without movies"),
    ("have a rewind button for your life", "have a pause button for your life"),
    ("speak every language", "play every instrument"),
    ("never use social media again", "never watch a video again"),
    ("be famous but broke", "be rich but unknown"),
]

TRUTHS = [
    "What's the most embarrassing thing you've done online?",
    "What's a habit of yours that you'd never admit in public?",
    "What's the last lie you told?",
    "Who in this server would you trust with a secret?",
    "What's your most unpopular opinion?",
    "What's the worst gift you've ever received?",
    "What's something you pretend to like but don't?",
    "What's the longest you've gone without showering?",
]

DARES = [
    "Change your nickname to whatever the next person says, for an hour.",
    "Send the last emoji you used five times in a row.",
    "Type the rest of your messages in ALL CAPS for 10 minutes.",
    "Talk like a pirate until someone asks you to stop.",
    "Say something nice about the last person who spoke.",
    "Post the 6th photo in your camera roll (nothing private).",
    "Describe your day using only emojis.",
]

ACTIONS = {
    "hug": ("hugs", "🤗"), "kiss": ("kisses", "😘"), "slap": ("slaps", "👋"), "pat": ("pats", "🫳"),
    "cuddle": ("cuddles", "🥰"), "poke": ("pokes", "👉"), "bite": ("bites", "😬"), "punch": ("punches", "👊"),
    "highfive": ("high-fives", "🙌"), "bonk": ("bonks", "🔨"),
}

RPS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def score_for(*parts: str) -> int:
    """A stable 0-100 number for the same inputs, so `rate` and `ship` don't change on every run."""
    key = "|".join(p.lower() for p in parts)
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % 101


def bar(pct: int, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


class Fun(BaseCog, name="Fun"):
    """Games and text toys"""

    help_text = (
        "# Fun\n"
        "\n"
        "## Games\n"
        "`{p}8ball <question>` - Ask the magic 8-ball (alias `8b`)\n"
        "`{p}coinflip` - Flip a coin (aliases `cf`, `flip`)\n"
        "`{p}roll [NdM]` - Roll dice, like `roll 2d6` (default `1d6`)\n"
        "`{p}choose a, b, c` - Pick one for you (alias `pick`)\n"
        "`{p}rps <rock|paper|scissors>` - Play against the bot\n"
        "`{p}ship @a [@b]` - How well do two people match? (alias `match`)\n"
        "`{p}rate <thing>` - Rate anything out of 100\n"
        "`{p}wyr` - A would-you-rather question\n"
        "`{p}truth` / `{p}dare` - A truth or a dare\n"
        "\n"
        "## Actions\n"
        "`{p}hug` `{p}kiss` `{p}slap` `{p}pat` `{p}cuddle` `{p}poke` `{p}bite` `{p}punch` `{p}highfive` `{p}bonk` "
        "`@user` - Do something to someone\n"
        "\n"
        "## Text\n"
        "`{p}mock <text>` - sPoNgEbOb tExT\n"
        "`{p}reverse <text>` - Reverse text\n"
        "`{p}clap <text>` - Put 👏 between 👏 words\n"
        "`{p}owo <text>` - Owoify text"
    )

    # ---- games -----------------------------------------------------------
    @commands.command(name="8ball", aliases=["8b"])
    async def eight_ball(self, ctx, *, question: str):
        """Ask the magic 8-ball"""
        e = discord.Embed(color=COLOR)
        e.add_field(name="Question", value=question[:500], inline=False)
        e.add_field(name="🎱", value=random.choice(EIGHT_BALL), inline=False)
        await ctx.send(embed=e, allowed_mentions=NO_PINGS)

    @commands.command(aliases=["cf", "flip"])
    async def coinflip(self, ctx):
        """Flip a coin"""
        await ctx.send(f"🪙 It landed on **{random.choice(['heads', 'tails'])}**.")

    @commands.command()
    async def roll(self, ctx, dice: str = "1d6"):
        """Roll dice, like 2d6"""
        m = re.fullmatch(r"(\d{1,2})?d(\d{1,4})", dice.lower())
        if not m:
            return await ctx.send("Use a format like `d20` or `2d6`.")
        count, sides = int(m[1] or 1), int(m[2])
        if count < 1 or sides < 2:
            return await ctx.send("Use at least 1 die with 2 sides.")
        rolls = [random.randint(1, sides) for _ in range(count)]
        shown = ", ".join(map(str, rolls))
        await ctx.send(f"🎲 {dice.lower()}: **{sum(rolls)}**" + (f" ({shown})" if count > 1 else ""))

    @commands.command(aliases=["pick"])
    async def choose(self, ctx, *, options: str):
        """Pick one: choose pizza, tacos, sushi"""
        items = [o.strip() for o in re.split(r",|\|", options) if o.strip()]
        if len(items) < 2:
            return await ctx.send("Give me at least two options, separated by commas.")
        await ctx.send(f"🤔 I pick **{random.choice(items)}**.", allowed_mentions=NO_PINGS)

    @commands.command()
    async def rps(self, ctx, choice: str):
        """Rock paper scissors"""
        choice = choice.lower()
        if choice not in RPS:
            return await ctx.send("Pick `rock`, `paper` or `scissors`.")
        bot_choice = random.choice(list(RPS))
        if choice == bot_choice:
            result = "It's a tie."
        elif BEATS[choice] == bot_choice:
            result = "You win!"
        else:
            result = "I win!"
        await ctx.send(f"{RPS[choice]} vs {RPS[bot_choice]} — {result}")

    @commands.command(aliases=["match"])
    async def ship(self, ctx, first: discord.Member, second: Optional[discord.Member] = None):
        """How well do two people match?"""
        second = second or ctx.author
        a, b = sorted((first.id, second.id))
        pct = score_for(str(a), str(b))
        e = discord.Embed(title="💘 Match", color=COLOR,
                          description=f"**{first.display_name}** + **{second.display_name}**\n"
                                      f"`{bar(pct)}` **{pct}%**")
        await ctx.send(embed=e, allowed_mentions=NO_PINGS)

    @commands.command()
    async def rate(self, ctx, *, thing: str):
        """Rate anything out of 100"""
        pct = score_for(thing)
        await ctx.send(f"I rate **{thing[:100]}** a **{pct}/100**. `{bar(pct)}`", allowed_mentions=NO_PINGS)

    @commands.command()
    async def wyr(self, ctx):
        """A would-you-rather question"""
        a, b = random.choice(WOULD_YOU_RATHER)
        msg = await ctx.send(embed=discord.Embed(title="Would you rather…", color=COLOR,
                                                 description=f"🅰️ {a}\n\n**or**\n\n🅱️ {b}"))
        for r in ("🅰️", "🅱️"):
            await msg.add_reaction(r)

    @commands.command()
    async def truth(self, ctx):
        """Get a truth question"""
        await ctx.send(embed=discord.Embed(title="Truth", description=random.choice(TRUTHS), color=COLOR))

    @commands.command()
    async def dare(self, ctx):
        """Get a dare"""
        await ctx.send(embed=discord.Embed(title="Dare", description=random.choice(DARES), color=COLOR))

    # ---- actions -----------------------------------------------------------
    async def _action(self, ctx, verb_emoji, member):
        verb, emoji = verb_emoji
        if member.id == ctx.author.id:
            return await ctx.send(f"{emoji} **{ctx.author.display_name}** {verb} themselves.", allowed_mentions=NO_PINGS)
        await ctx.send(f"{emoji} **{ctx.author.display_name}** {verb} **{member.display_name}**.",
                       allowed_mentions=NO_PINGS)

    @commands.command()
    async def hug(self, ctx, member: discord.Member):
        """Hug someone"""
        await self._action(ctx, ACTIONS["hug"], member)

    @commands.command()
    async def kiss(self, ctx, member: discord.Member):
        """Kiss someone"""
        await self._action(ctx, ACTIONS["kiss"], member)

    @commands.command()
    async def slap(self, ctx, member: discord.Member):
        """Slap someone"""
        await self._action(ctx, ACTIONS["slap"], member)

    @commands.command()
    async def pat(self, ctx, member: discord.Member):
        """Pat someone"""
        await self._action(ctx, ACTIONS["pat"], member)

    @commands.command()
    async def cuddle(self, ctx, member: discord.Member):
        """Cuddle someone"""
        await self._action(ctx, ACTIONS["cuddle"], member)

    @commands.command()
    async def poke(self, ctx, member: discord.Member):
        """Poke someone"""
        await self._action(ctx, ACTIONS["poke"], member)

    @commands.command()
    async def bite(self, ctx, member: discord.Member):
        """Bite someone"""
        await self._action(ctx, ACTIONS["bite"], member)

    @commands.command()
    async def punch(self, ctx, member: discord.Member):
        """Punch someone"""
        await self._action(ctx, ACTIONS["punch"], member)

    @commands.command()
    async def highfive(self, ctx, member: discord.Member):
        """High-five someone"""
        await self._action(ctx, ACTIONS["highfive"], member)

    @commands.command()
    async def bonk(self, ctx, member: discord.Member):
        """Bonk someone"""
        await self._action(ctx, ACTIONS["bonk"], member)

    # ---- text toys -----------------------------------------------------------
    @commands.command()
    async def mock(self, ctx, *, text: str):
        """sPoNgEbOb tExT"""
        out, up = [], False
        for ch in text[:500]:
            if ch.isalpha():
                out.append(ch.upper() if up else ch.lower())
                up = not up
            else:
                out.append(ch)
        await ctx.send("".join(out), allowed_mentions=NO_PINGS)

    @commands.command()
    async def reverse(self, ctx, *, text: str):
        """Reverse text"""
        await ctx.send(text[:500][::-1], allowed_mentions=NO_PINGS)

    @commands.command()
    async def clap(self, ctx, *, text: str):
        """Put 👏 between 👏 words"""
        await ctx.send(" 👏 ".join(text.split())[:1900], allowed_mentions=NO_PINGS)

    @commands.command()
    async def owo(self, ctx, *, text: str):
        """Owoify text"""
        t = text[:500]
        t = re.sub(r"[rl]", "w", t)
        t = re.sub(r"[RL]", "W", t)
        t = re.sub(r"n([aeiou])", r"ny\1", t)
        t = re.sub(r"N([aeiouAEIOU])", r"Ny\1", t)
        t = t.replace("ove", "uv")
        await ctx.send(f"{t} {random.choice(['owo', 'uwu', '>w<', '^w^'])}", allowed_mentions=NO_PINGS)


async def setup(bot):
    add_cog_safely(bot, Fun())
