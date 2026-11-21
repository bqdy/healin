"""Utility cog: info lookups and everyday tools (the bleed-style ones).

Commands whose name another cog already owns are skipped automatically, so this can't break
anything that already works.
"""
import ast
import asyncio
import operator
import platform
import re
import time
from datetime import datetime
from typing import Optional, Union

import discord
from discord.ext import commands

from utils.base import COLOR, NO_PINGS, BaseCog
from utils.paginator import chunk_pages, paginate
from utils.safe_cog import add_cog_safely
from utils.store import GuildStore

EMOJI_RE = re.compile(r"<(a?):(\w{2,32}):(\d{15,25})>")
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
RESERVED_TAGS = {"add", "create", "remove", "delete", "list", "info"}
UserLike = Union[discord.Member, discord.User]

# ---- safe calculator ---------------------------------------------------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(expr: str):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")

    return ev(ast.parse(expr.replace("^", "**").replace("x", "*").replace(",", ""), mode="eval"))


def parse_birthday(text: str) -> Optional[str]:
    """'12/25', '12-25', 'dec 25', '25 december' -> '12-25' (or None)."""
    text = re.sub(r"\s+", " ", text.strip())
    for fmt in ("%m/%d", "%m-%d", "%b %d", "%B %d", "%d %b", "%d %B"):
        try:
            d = datetime.strptime(f"{text} 2000", f"{fmt} %Y")
            return f"{d.month:02d}-{d.day:02d}"
        except ValueError:
            continue
    return None


def pretty_birthday(key: str) -> str:
    m, d = key.split("-")
    return datetime(2000, int(m), int(d)).strftime("%B %d").replace(" 0", " ")


def fmt_span(seconds: float) -> str:
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{n}{u}" for n, u in ((d, "d"), (h, "h"), (m, "m")) if n]
    return " ".join(parts) or f"{s}s"


class Utility(BaseCog, name="Utility"):
    """Info lookups and everyday tools"""

    help_text = [
        "# Utility\n"
        "\n"
        "## Info\n"
        "`{p}avatar [user]` - Show someone's avatar (aliases `av`, `pfp`)\n"
        "`{p}serveravatar [member]` - Their server avatar (alias `sav`)\n"
        "`{p}banner [user]` - Show someone's profile banner\n"
        "`{p}servericon` / `{p}serverbanner` - The server's icon or banner\n"
        "`{p}userinfo [user]` - Info about a user (aliases `ui`, `uinfo`)\n"
        "`{p}serverinfo` - Info about this server (alias `si`)\n"
        "`{p}channelinfo [channel]` - Info about a channel (alias `ci`)\n"
        "`{p}inviteinfo <invite>` - Look up a server invite\n"
        "`{p}botinfo` - About the bot (aliases `about`, `bi`)\n"
        "`{p}uptime` - How long the bot has been online\n"
        "`{p}invite` - Get the bot's invite link\n"
        "`{p}joinposition [member]` - Where someone joined (alias `joinpos`)\n"
        "`{p}firstmessage [#channel]` - Jump to a channel's first message (alias `firstmsg`)\n"
        "`{p}boosters` - List server boosters\n"
        "`{p}bans` - List banned users (Ban Members)\n"
        "`{p}emojis` - List the server's emojis\n"
        "\n"
        "## Tools\n"
        "`{p}calc <math>` - Calculator (alias `calculate`)\n"
        "`{p}poll <question> | <option> | <option>` - Start a poll (no options = yes/no)\n"
        "`{p}afk [reason]` - Set yourself AFK, cleared when you next talk\n"
        "`{p}enlarge <emoji>` - Big version of a custom emoji (alias `jumbo`)\n"
        "`{p}steal <emoji> [name]` - Add a custom emoji here, or reply to a message (Manage Expressions)\n"
        "`{p}selfpurge [amount]` - Delete your own recent messages here (alias `sp`)\n"
        "`{p}pin` / `{p}unpin` - Reply to a message to pin or unpin it (Manage Messages)\n"
        "`{p}nsfw` - Toggle the age restriction on this channel (Manage Channels)",

        "# Utility\n"
        "\n"
        "## Tags\n"
        "`{p}tag <name>` - Show a tag\n"
        "`{p}tag add <name> <content>` - Create a tag (use quotes for a name with spaces)\n"
        "`{p}tag remove <name>` - Delete your tag (Manage Messages can delete any)\n"
        "`{p}tag list` - List this server's tags\n"
        "`{p}tag info <name>` - Who made a tag and how often it's used\n"
        "\n"
        "## Birthdays\n"
        "`{p}birthday [member]` - View a birthday (alias `bday`)\n"
        "`{p}birthday set <date>` - Set yours, like `12/25` or `dec 25`\n"
        "`{p}birthday remove` - Remove yours\n"
        "`{p}birthday list` - Upcoming birthdays in this server",
    ]

    def __init__(self, bot):
        self.bot = bot
        self.store = GuildStore("utility", {"afk": {}, "tags": {}, "birthdays": {}})
        self.started = discord.utils.utcnow()

    # ======================================================================
    # AVATARS AND BANNERS
    # ======================================================================
    @staticmethod
    def _asset_links(asset: discord.Asset) -> str:
        links = [f"[{f}]({asset.with_format(f).url})" for f in ("png", "jpg", "webp")]
        if asset.is_animated():
            links.append(f"[gif]({asset.with_format('gif').url})")
        return " • ".join(links)

    @commands.command(aliases=["av", "pfp"])
    async def avatar(self, ctx, user: Optional[UserLike] = None):
        """Show someone's avatar"""
        user = user or ctx.author
        asset = user.avatar or user.default_avatar
        e = discord.Embed(title=f"{user.name}'s avatar", description=self._asset_links(asset), color=COLOR)
        e.set_image(url=asset.url)
        await ctx.send(embed=e)

    @commands.command(aliases=["sav"])
    async def serveravatar(self, ctx, member: Optional[discord.Member] = None):
        """Show someone's server-specific avatar"""
        member = member or ctx.author
        if not member.guild_avatar:
            return await ctx.send(f"**{member.name}** doesn't have a server avatar.", allowed_mentions=NO_PINGS)
        asset = member.guild_avatar
        e = discord.Embed(title=f"{member.name}'s server avatar", description=self._asset_links(asset), color=COLOR)
        e.set_image(url=asset.url)
        await ctx.send(embed=e)

    @commands.command()
    async def banner(self, ctx, user: Optional[UserLike] = None):
        """Show someone's profile banner"""
        user = user or ctx.author
        fetched = await self.bot.fetch_user(user.id)
        if not fetched.banner:
            return await ctx.send(f"**{user.name}** doesn't have a banner.", allowed_mentions=NO_PINGS)
        e = discord.Embed(title=f"{user.name}'s banner", description=self._asset_links(fetched.banner), color=COLOR)
        e.set_image(url=fetched.banner.url)
        await ctx.send(embed=e)

    @commands.command(aliases=["guildicon"])
    async def servericon(self, ctx):
        """Show the server icon"""
        if not ctx.guild.icon:
            return await ctx.send("This server has no icon.")
        e = discord.Embed(title=f"{ctx.guild.name}'s icon", description=self._asset_links(ctx.guild.icon), color=COLOR)
        e.set_image(url=ctx.guild.icon.url)
        await ctx.send(embed=e)

    @commands.command(aliases=["guildbanner"])
    async def serverbanner(self, ctx):
        """Show the server banner"""
        if not ctx.guild.banner:
            return await ctx.send("This server has no banner.")
        e = discord.Embed(title=f"{ctx.guild.name}'s banner", description=self._asset_links(ctx.guild.banner), color=COLOR)
        e.set_image(url=ctx.guild.banner.url)
        await ctx.send(embed=e)

    # ======================================================================
    # INFO
    # ======================================================================
    @commands.command(name="userinfo", aliases=["ui", "uinfo"])
    async def userinfo(self, ctx, user: Optional[UserLike] = None):
        """Info about a user"""
        user = user or ctx.author
        member = user if isinstance(user, discord.Member) else None
        color = member.color if member and member.color.value else discord.Colour(COLOR)
        e = discord.Embed(color=color)
        e.set_author(name=f"{user} ({user.id})", icon_url=(user.avatar or user.default_avatar).url)
        e.set_thumbnail(url=(user.avatar or user.default_avatar).url)
        e.add_field(name="Created", value=f"{discord.utils.format_dt(user.created_at, 'F')}\n"
                                          f"{discord.utils.format_dt(user.created_at, 'R')}", inline=False)
        if member:
            if member.joined_at:
                e.add_field(name="Joined", value=f"{discord.utils.format_dt(member.joined_at, 'F')}\n"
                                                 f"{discord.utils.format_dt(member.joined_at, 'R')}", inline=False)
            if member.premium_since:
                e.add_field(name="Boosting since", value=discord.utils.format_dt(member.premium_since, "R"))
            roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
            if roles:
                shown = " ".join(roles[:20]) + (f" +{len(roles) - 20} more" if len(roles) > 20 else "")
                e.add_field(name=f"Roles ({len(roles)})", value=shown[:1024], inline=False)
        if user.bot:
            e.set_footer(text="Bot account")
        await ctx.send(embed=e)

    @commands.command(aliases=["si", "guildinfo"])
    async def serverinfo(self, ctx):
        """Info about this server"""
        g = ctx.guild
        humans = sum(1 for m in g.members if not m.bot)
        e = discord.Embed(title=g.name, description=g.description, color=COLOR)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        if g.banner:
            e.set_image(url=g.banner.url)
        e.add_field(name="Owner", value=f"<@{g.owner_id}>")
        e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
        e.add_field(name="ID", value=str(g.id))
        e.add_field(name="Members", value=f"{g.member_count} ({humans} humans, {g.member_count - humans} bots)")
        e.add_field(name="Channels", value=f"{len(g.text_channels)} text, {len(g.voice_channels)} voice, "
                                           f"{len(g.categories)} categories")
        e.add_field(name="Roles", value=str(len(g.roles) - 1))
        e.add_field(name="Emojis", value=f"{len(g.emojis)}/{g.emoji_limit}")
        e.add_field(name="Boosts", value=f"Level {g.premium_tier} ({g.premium_subscription_count} boosts)")
        e.add_field(name="Verification", value=str(g.verification_level).title())
        await ctx.send(embed=e, allowed_mentions=NO_PINGS)

    @commands.command(aliases=["ci", "chinfo"])
    async def channelinfo(self, ctx, channel: Optional[discord.abc.GuildChannel] = None):
        """Info about a channel"""
        ch = channel or ctx.channel
        e = discord.Embed(title=f"#{ch.name}", color=COLOR)
        e.add_field(name="ID", value=str(ch.id))
        e.add_field(name="Type", value=str(ch.type).replace("_", " ").title())
        e.add_field(name="Created", value=discord.utils.format_dt(ch.created_at, "R"))
        if ch.category:
            e.add_field(name="Category", value=ch.category.name)
        if isinstance(ch, discord.TextChannel):
            e.add_field(name="Slowmode", value=f"{ch.slowmode_delay}s" if ch.slowmode_delay else "Off")
            e.add_field(name="NSFW", value="Yes" if ch.is_nsfw() else "No")
            if ch.topic:
                e.add_field(name="Topic", value=ch.topic[:1024], inline=False)
        elif isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            e.add_field(name="Bitrate", value=f"{ch.bitrate // 1000}kbps")
            e.add_field(name="User limit", value=str(ch.user_limit or "None"))
        await ctx.send(embed=e)

    @commands.command()
    async def inviteinfo(self, ctx, invite: str):
        """Look up a server invite"""
        try:
            inv = await self.bot.fetch_invite(invite, with_counts=True)
        except discord.NotFound:
            return await ctx.send("I couldn't find that invite.")
        e = discord.Embed(title=inv.guild.name if inv.guild else "Unknown server", color=COLOR)
        if inv.guild and inv.guild.icon:
            e.set_thumbnail(url=inv.guild.icon.url)
        e.add_field(name="Code", value=inv.code)
        if inv.channel:
            e.add_field(name="Channel", value=inv.channel.name)
        if inv.inviter:
            e.add_field(name="Inviter", value=str(inv.inviter))
        if inv.approximate_member_count is not None:
            e.add_field(name="Members", value=f"{inv.approximate_member_count} ({inv.approximate_presence_count} online)")
        await ctx.send(embed=e)

    @commands.command(aliases=["about", "bi"])
    async def botinfo(self, ctx):
        """About the bot"""
        b = self.bot
        e = discord.Embed(title=getattr(b, "bot_name", b.user.name), color=COLOR)
        e.set_thumbnail(url=b.user.display_avatar.url)
        e.add_field(name="Servers", value=str(len(b.guilds)))
        e.add_field(name="Users", value=str(sum(g.member_count or 0 for g in b.guilds)))
        e.add_field(name="Commands", value=str(len(list(b.walk_commands()))))
        e.add_field(name="Latency", value=f"{round(b.latency * 1000)}ms")
        e.add_field(name="Uptime", value=fmt_span((discord.utils.utcnow() - self.started).total_seconds()))
        e.add_field(name="Versions", value=f"discord.py {discord.__version__}, Python {platform.python_version()}")
        if getattr(b, "credit", ""):
            e.set_footer(text=f"Created by {b.credit}")
        await ctx.send(embed=e)

    @commands.command()
    async def uptime(self, ctx):
        """How long the bot has been online"""
        await ctx.send(f"⏱️ Online for **{fmt_span((discord.utils.utcnow() - self.started).total_seconds())}**.")

    @commands.command()
    async def invite(self, ctx):
        """Get the bot's invite link"""
        url = discord.utils.oauth_url(self.bot.user.id, permissions=discord.Permissions(administrator=True),
                                      scopes=("bot", "applications.commands"))
        await ctx.send(embed=discord.Embed(description=f"[Click here to invite me]({url})", color=COLOR))

    @commands.command(aliases=["joinpos"])
    async def joinposition(self, ctx, member: Optional[discord.Member] = None):
        """Where a member joined, counting from the first member"""
        member = member or ctx.author
        ordered = sorted((m for m in ctx.guild.members if m.joined_at),
                         key=lambda m: m.joined_at)
        try:
            pos = ordered.index(member) + 1
        except ValueError:
            return await ctx.send("I couldn't work that out.")
        await ctx.send(f"**{member.name}** was member **#{pos}** of {len(ordered)}.", allowed_mentions=NO_PINGS)

    @commands.command(aliases=["firstmsg"])
    async def firstmessage(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Jump to the first message in a channel"""
        channel = channel or ctx.channel
        async for msg in channel.history(limit=1, oldest_first=True):
            e = discord.Embed(description=msg.content[:300] or "*no text*", color=COLOR,
                              timestamp=msg.created_at)
            e.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
            e.add_field(name="Jump", value=f"[Go to message]({msg.jump_url})")
            return await ctx.send(embed=e)
        await ctx.send("That channel has no messages.")

    @commands.command()
    async def boosters(self, ctx):
        """List server boosters"""
        subs = sorted((m for m in ctx.guild.premium_subscribers if m.premium_since),
                      key=lambda m: m.premium_since)
        lines = [f"`{i}.` {m.mention} — {discord.utils.format_dt(m.premium_since, 'R')}"
                 for i, m in enumerate(subs, 1)]
        await paginate(ctx, chunk_pages(f"Boosters ({len(subs)})", lines, COLOR, empty="Nobody is boosting."))

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def bans(self, ctx):
        """List banned users"""
        lines = []
        async for entry in ctx.guild.bans(limit=None):
            reason = (entry.reason or "No reason").replace("\n", " ")[:60]
            lines.append(f"`{entry.user.id}` **{entry.user}** — {reason}")
        await paginate(ctx, chunk_pages(f"Bans ({len(lines)})", lines, COLOR, empty="Nobody is banned."))

    @commands.command()
    async def emojis(self, ctx):
        """List the server's emojis"""
        lines = [f"{e} `:{e.name}:`" for e in ctx.guild.emojis]
        await paginate(ctx, chunk_pages(f"Emojis ({len(lines)})", lines, COLOR, per_page=20,
                                        empty="This server has no custom emojis."))

    # ======================================================================
    # TOOLS
    # ======================================================================
    @commands.command(aliases=["calculate"])
    async def calc(self, ctx, *, expression: str):
        """Calculator: + - * / // % ** and parentheses"""
        try:
            result = safe_eval(expression)
        except ZeroDivisionError:
            return await ctx.send("You can't divide by zero.")
        except (ValueError, SyntaxError, OverflowError, TypeError):
            return await ctx.send("I can't work that out. Use numbers with `+ - * / // % **` and brackets.")
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        await ctx.send(f"🧮 `{expression}` = **{result:,}**" if isinstance(result, int)
                       else f"🧮 `{expression}` = **{result}**", allowed_mentions=NO_PINGS)

    @commands.command()
    async def poll(self, ctx, *, text: str):
        """Start a poll: question | option | option (no options = yes/no)"""
        parts = [p.strip() for p in text.split("|") if p.strip()]
        if not parts:
            return await ctx.send("Give me a question.")
        question, options = parts[0], parts[1:]
        if len(options) == 1 or len(options) > 10:
            return await ctx.send("Give 2 to 10 options, or none for a yes/no poll.")
        e = discord.Embed(title=question[:250], color=COLOR)
        e.set_footer(text=f"Poll by {ctx.author.name}")
        if options:
            e.description = "\n".join(f"{NUMS[i]} {o}" for i, o in enumerate(options))
            reactions = NUMS[:len(options)]
        else:
            reactions = ["✅", "❌"]
        msg = await ctx.send(embed=e)
        for r in reactions:
            await msg.add_reaction(r)

    @commands.command(aliases=["jumbo"])
    async def enlarge(self, ctx, emoji: str):
        """Show a bigger version of a custom emoji"""
        m = EMOJI_RE.fullmatch(emoji.strip())
        if not m:
            return await ctx.send("Use a custom emoji from a server.")
        partial = discord.PartialEmoji(name=m[2], animated=bool(m[1]), id=int(m[3]))
        e = discord.Embed(title=f":{partial.name}:", color=COLOR)
        e.set_image(url=partial.url)
        await ctx.send(embed=e)

    @commands.command()
    @commands.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def steal(self, ctx, emoji: Optional[str] = None, name: Optional[str] = None):
        """Add a custom emoji to this server (give one, or reply to a message that has some)"""
        found = []
        if emoji and EMOJI_RE.fullmatch(emoji.strip()):
            found = [EMOJI_RE.fullmatch(emoji.strip())]
        elif ctx.message.reference and ctx.message.reference.message_id:
            ref = ctx.message.reference.resolved
            if not isinstance(ref, discord.Message):
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            found = list(EMOJI_RE.finditer(ref.content))
        if not found:
            return await ctx.send("Give me a custom emoji, or reply to a message that has one.")
        made = []
        for m in found[:10]:
            partial = discord.PartialEmoji(name=m[2], animated=bool(m[1]), id=int(m[3]))
            data = await partial.read()
            new = await ctx.guild.create_custom_emoji(
                name=(name if name and len(found) == 1 else m[2])[:32], image=data,
                reason=f"steal by {ctx.author}")
            made.append(str(new))
        await ctx.send("Added " + " ".join(made))

    @commands.command(aliases=["sp"])
    async def selfpurge(self, ctx, amount: int = 10):
        """Delete your own recent messages in this channel (up to 50)"""
        amount = max(1, min(amount, 50))
        removed = 0
        async for m in ctx.channel.history(limit=300):
            if m.author.id != ctx.author.id:
                continue
            try:
                await m.delete()
            except discord.HTTPException:
                continue
            removed += 1
            if removed >= amount:
                break
        await ctx.send(f"🧹 Deleted {removed} of your message(s).", delete_after=5)

    async def _replied(self, ctx) -> Optional[discord.Message]:
        ref = ctx.message.reference
        if not ref or not ref.message_id:
            await ctx.send("Reply to the message you want, then run the command.")
            return None
        return await ctx.channel.fetch_message(ref.message_id)

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def pin(self, ctx):
        """Pin the message you're replying to"""
        msg = await self._replied(ctx)
        if msg:
            await msg.pin(reason=f"Pinned by {ctx.author}")
            await ctx.message.add_reaction("📌")

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def unpin(self, ctx):
        """Unpin the message you're replying to"""
        msg = await self._replied(ctx)
        if msg:
            await msg.unpin(reason=f"Unpinned by {ctx.author}")
            await ctx.message.add_reaction("✅")

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    async def nsfw(self, ctx):
        """Toggle the age restriction on this channel"""
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.send("Only text channels can be age-restricted.")
        new = not ctx.channel.is_nsfw()
        await ctx.channel.edit(nsfw=new, reason=f"nsfw toggled by {ctx.author}")
        await ctx.send("🔞 This channel is now age-restricted." if new else "✅ This channel is no longer age-restricted.")

    # ======================================================================
    # AFK
    # ======================================================================
    @commands.command()
    async def afk(self, ctx, *, reason: str = "AFK"):
        """Set yourself AFK. It clears the next time you talk."""
        cfg = self.store.get(ctx.guild.id)
        cfg["afk"][str(ctx.author.id)] = {"reason": reason[:200], "at": int(time.time())}
        self.store.save()
        await ctx.send(f"💤 {ctx.author.mention}: you're now AFK — {reason[:200]}", allowed_mentions=NO_PINGS)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        afk = self.store.get(message.guild.id)["afk"]
        if not afk:
            return
        prefix = self.bot.guild_prefix(message.guild) if hasattr(self.bot, "guild_prefix") else "-"
        is_afk_cmd = message.content.lower().startswith(f"{prefix.lower()}afk")
        uid = str(message.author.id)
        if uid in afk and not is_afk_cmd:
            data = afk.pop(uid)
            self.store.save()
            await message.channel.send(
                f"👋 Welcome back {message.author.mention}, you were AFK for {fmt_span(time.time() - data['at'])}.",
                delete_after=10, allowed_mentions=NO_PINGS)
        shown = 0
        for m in message.mentions:
            data = afk.get(str(m.id))
            if data and m.id != message.author.id and shown < 3:
                shown += 1
                await message.channel.send(
                    f"💤 **{m.display_name}** is AFK: {data['reason']} — <t:{data['at']}:R>",
                    delete_after=15, allowed_mentions=NO_PINGS)

    # ======================================================================
    # TAGS
    # ======================================================================
    @commands.group(invoke_without_command=True)
    async def tag(self, ctx, *, name: str):
        """Show a tag"""
        tags = self.store.get(ctx.guild.id)["tags"]
        t = tags.get(name.lower())
        if not t:
            return await ctx.send(f"There's no tag called `{name[:50]}`.", allowed_mentions=NO_PINGS)
        t["uses"] = t.get("uses", 0) + 1
        self.store.save()
        await ctx.send(t["content"], allowed_mentions=NO_PINGS)

    @tag.command(name="add", aliases=["create"])
    async def tag_add(self, ctx, name: str, *, content: str):
        """Create a tag"""
        name = name.lower()
        tags = self.store.get(ctx.guild.id)["tags"]
        if name in RESERVED_TAGS:
            return await ctx.send("You can't use that name.")
        if len(name) > 50 or len(content) > 1800:
            return await ctx.send("Name up to 50 characters and content up to 1800.")
        if name in tags:
            return await ctx.send("That tag already exists.")
        if len(tags) >= 100:
            return await ctx.send("This server has hit the 100 tag limit.")
        tags[name] = {"content": content, "owner": ctx.author.id, "uses": 0, "at": int(time.time())}
        self.store.save()
        await ctx.send(f"✅ Created tag `{name}`.", allowed_mentions=NO_PINGS)

    @tag.command(name="remove", aliases=["delete"])
    async def tag_remove(self, ctx, *, name: str):
        """Delete a tag"""
        tags = self.store.get(ctx.guild.id)["tags"]
        t = tags.get(name.lower())
        if not t:
            return await ctx.send("There's no tag with that name.")
        if t["owner"] != ctx.author.id and not ctx.author.guild_permissions.manage_messages:
            return await ctx.send("You can only delete your own tags.")
        del tags[name.lower()]
        self.store.save()
        await ctx.send(f"🗑️ Deleted tag `{name.lower()}`.", allowed_mentions=NO_PINGS)

    @tag.command(name="list")
    async def tag_list(self, ctx):
        """List this server's tags"""
        tags = self.store.get(ctx.guild.id)["tags"]
        lines = [f"`{n}` — {t.get('uses', 0)} use(s)" for n, t in sorted(tags.items())]
        await paginate(ctx, chunk_pages(f"Tags ({len(lines)})", lines, COLOR, empty="No tags yet."))

    @tag.command(name="info")
    async def tag_info(self, ctx, *, name: str):
        """Who made a tag and how often it's used"""
        t = self.store.get(ctx.guild.id)["tags"].get(name.lower())
        if not t:
            return await ctx.send("There's no tag with that name.")
        e = discord.Embed(title=name.lower(), color=COLOR)
        e.add_field(name="Owner", value=f"<@{t['owner']}>")
        e.add_field(name="Uses", value=str(t.get("uses", 0)))
        e.add_field(name="Created", value=f"<t:{t['at']}:R>")
        await ctx.send(embed=e, allowed_mentions=NO_PINGS)

    # ======================================================================
    # BIRTHDAYS
    # ======================================================================
    @commands.group(invoke_without_command=True, aliases=["bday"])
    async def birthday(self, ctx, member: Optional[discord.Member] = None):
        """View a birthday"""
        member = member or ctx.author
        key = self.store.get(ctx.guild.id)["birthdays"].get(str(member.id))
        if not key:
            return await ctx.send(f"**{member.name}** hasn't set a birthday.", allowed_mentions=NO_PINGS)
        await ctx.send(f"🎂 **{member.name}**'s birthday is **{pretty_birthday(key)}**.", allowed_mentions=NO_PINGS)

    @birthday.command(name="set")
    async def birthday_set(self, ctx, *, date: str):
        """Set your birthday, like 12/25 or dec 25"""
        key = parse_birthday(date)
        if not key:
            return await ctx.send("I couldn't read that date. Try `12/25` or `dec 25`.")
        self.store.get(ctx.guild.id)["birthdays"][str(ctx.author.id)] = key
        self.store.save()
        await ctx.send(f"🎂 Saved your birthday as **{pretty_birthday(key)}**.")

    @birthday.command(name="remove")
    async def birthday_remove(self, ctx):
        """Remove your birthday"""
        b = self.store.get(ctx.guild.id)["birthdays"]
        if b.pop(str(ctx.author.id), None) is None:
            return await ctx.send("You haven't set a birthday.")
        self.store.save()
        await ctx.send("🗑️ Removed your birthday.")

    @birthday.command(name="list")
    async def birthday_list(self, ctx):
        """Upcoming birthdays in this server"""
        b = self.store.get(ctx.guild.id)["birthdays"]
        today = discord.utils.utcnow().date()

        def days_until(key):
            m, d = map(int, key.split("-"))
            for year in (today.year, today.year + 1):
                try:
                    delta = (datetime(year, m, d).date() - today).days
                except ValueError:  # Feb 29 in a non-leap year
                    continue
                if delta >= 0:
                    return delta
            return 999

        rows = sorted(((days_until(k), uid, k) for uid, k in b.items() if ctx.guild.get_member(int(uid))))
        lines = [f"<@{uid}> — {pretty_birthday(k)}" + (" 🎉 **today**" if n == 0 else f" (in {n}d)")
                 for n, uid, k in rows]
        await paginate(ctx, chunk_pages("Birthdays", lines, COLOR, empty="Nobody has set a birthday."))


async def setup(bot):
    add_cog_safely(bot, Utility(bot))
