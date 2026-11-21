"""Economy cog: a small per-server currency with daily rewards, work and a few games.

Money lives in data/economy.json (one wallet and bank per member per server).
"""
import os
import random
import time
from typing import Optional

import discord
from discord.ext import commands

from utils.base import COLOR, NO_PINGS, BaseCog
from utils.paginator import chunk_pages, paginate
from utils.safe_cog import add_cog_safely
from utils.store import GuildStore

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "482506940027502597"))

CUR = "💵"
DAILY_AMOUNT = 500
DAILY_COOLDOWN = 24 * 3600
WORK_COOLDOWN = 3600
MAX_BET = 1_000_000
SHARED_KEY = "shared"  # one economy for every server
WEEKLY_AMOUNT = 3500
WEEKLY_COOLDOWN = 7 * 24 * 3600
ROB_COOLDOWN = 2 * 3600
ROB_SUCCESS = 0.4

SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "🔔", "💎", "7️⃣"]
WORK_LINES = [
    "You delivered pizzas", "You walked some dogs", "You fixed a neighbour's wifi",
    "You streamed for three viewers", "You washed cars", "You sold lemonade", "You mowed lawns",
]


def fmt(n: int) -> str:
    return f"{CUR} {n:,}"


def bot_owner_only():
    """Only the bot owner (BOT_OWNER_ID) may run the command."""
    async def predicate(ctx):
        if ctx.author.id != BOT_OWNER_ID:
            raise commands.NotOwner("Only the bot owner can use that.")
        return True
    return commands.check(predicate)


class Economy(BaseCog, name="Economy"):
    """Money, daily rewards and gambling"""

    help_text = (
        "# Economy\n"
        "Your money is the same in every server.\n"
        "\n"
        "## Money\n"
        "`{p}balance [member]` - See a wallet and bank (aliases `bal`, `cash`)\n"
        "`{p}daily` - Claim your daily reward\n"
        "`{p}weekly` - Claim your weekly reward\n"
        "`{p}work` - Work for some cash (once an hour)\n"
        "`{p}pay @member <amount>` - Send money (alias `give`)\n"
        "`{p}deposit <amount|all>` - Move cash to your bank (alias `dep`)\n"
        "`{p}withdraw <amount|all>` - Take cash out of your bank (alias `with`)\n"
        "`{p}rob @member` - Try to steal some cash (once every 2 hours, alias `steal`)\n"
        "`{p}baltop` - Richest users across all servers (alias `richest`)\n"
        "\n"
        "## Games\n"
        "`{p}gamble <amount|all>` - Double it or lose it (alias `bet`)\n"
        "`{p}slots <amount|all>` - Spin the slot machine\n"
        "`{p}coinflip <heads|tails> <amount|all>` - Call it, double or lose (alias `cf`)\n"
        "\n"
        "## Bot owner only\n"
        "`{p}economy add @member <amount>` - Give money\n"
        "`{p}economy remove @member <amount>` - Take money\n"
        "`{p}economy reset @member` - Wipe someone's money"
    )

    def __init__(self, bot):
        self.bot = bot
        self.store = GuildStore("economy", {"users": {}})
        self._merge_old_servers()

    # ---- data helpers ------------------------------------------------------
    def users(self) -> dict:
        """Every wallet, shared across all servers."""
        return self.store.get(SHARED_KEY)["users"]

    def wallet(self, user_id: int) -> dict:
        w = self.users().setdefault(str(user_id), {"cash": 0, "bank": 0, "daily": 0, "work": 0})
        for k in ("weekly", "rob"):  # fields added later; older wallets won't have them
            w.setdefault(k, 0)
        return w

    def _merge_old_servers(self) -> None:
        """One-time: fold the old per-server wallets into the shared one (balances add up)."""
        old = [k for k in self.store.data if k != SHARED_KEY]
        if not old:
            return
        shared = self.users()
        for gid in old:
            for uid, w in self.store.data.pop(gid, {}).get("users", {}).items():
                mine = shared.setdefault(uid, {"cash": 0, "bank": 0, "daily": 0, "work": 0})
                mine["cash"] += w.get("cash", 0)
                mine["bank"] += w.get("bank", 0)
                for k in ("daily", "work", "weekly", "rob"):
                    mine[k] = max(mine.get(k, 0), w.get(k, 0))
        self.store.save()

    @staticmethod
    def amount(text: str, available: int) -> int:
        if text.lower() == "all":
            return available
        text = text.lower().replace(",", "")
        mult = 1
        if text.endswith("k"):
            mult, text = 1_000, text[:-1]
        elif text.endswith("m"):
            mult, text = 1_000_000, text[:-1]
        try:
            return int(float(text) * mult)
        except ValueError:
            raise commands.BadArgument("amount")

    async def _bet(self, ctx, text: str):
        w = self.wallet(ctx.author.id)
        bet = self.amount(text, min(w["cash"], MAX_BET))
        if bet < 1:
            await ctx.send("Your bet has to be at least 1.")
            return None
        if bet > MAX_BET:
            await ctx.send(f"The max bet is {fmt(MAX_BET)}.")
            return None
        if bet > w["cash"]:
            await ctx.send(f"You only have {fmt(w['cash'])} in cash.")
            return None
        return w, bet

    # ---- money ---------------------------------------------------------------
    @commands.command(aliases=["bal", "cash"])
    async def balance(self, ctx, member: Optional[discord.Member] = None):
        """See a wallet and bank"""
        member = member or ctx.author
        w = self.wallet(member.id)
        e = discord.Embed(title=f"{member.display_name}'s balance", color=COLOR)
        e.add_field(name="Cash", value=fmt(w["cash"]))
        e.add_field(name="Bank", value=fmt(w["bank"]))
        e.add_field(name="Total", value=fmt(w["cash"] + w["bank"]))
        e.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=e, allowed_mentions=NO_PINGS)

    @commands.command()
    async def daily(self, ctx):
        """Claim your daily reward"""
        w = self.wallet(ctx.author.id)
        left = w["daily"] + DAILY_COOLDOWN - time.time()
        if left > 0:
            return await ctx.send(f"⏳ Come back <t:{int(time.time() + left)}:R> for your next daily.")
        w["cash"] += DAILY_AMOUNT
        w["daily"] = int(time.time())
        self.store.save()
        await ctx.send(f"✅ You claimed your daily {fmt(DAILY_AMOUNT)}.")

    @commands.command()
    async def weekly(self, ctx):
        """Claim your weekly reward"""
        w = self.wallet(ctx.author.id)
        left = w["weekly"] + WEEKLY_COOLDOWN - time.time()
        if left > 0:
            return await ctx.send(f"⏳ Come back <t:{int(time.time() + left)}:R> for your next weekly.")
        w["cash"] += WEEKLY_AMOUNT
        w["weekly"] = int(time.time())
        self.store.save()
        await ctx.send(f"✅ You claimed your weekly {fmt(WEEKLY_AMOUNT)}.")

    @commands.command()
    async def work(self, ctx):
        """Work for some cash (once an hour)"""
        w = self.wallet(ctx.author.id)
        left = w["work"] + WORK_COOLDOWN - time.time()
        if left > 0:
            return await ctx.send(f"⏳ You can work again <t:{int(time.time() + left)}:R>.")
        earned = random.randint(50, 250)
        w["cash"] += earned
        w["work"] = int(time.time())
        self.store.save()
        await ctx.send(f"💼 {random.choice(WORK_LINES)} and earned {fmt(earned)}.")

    @commands.command(aliases=["give"])
    async def pay(self, ctx, member: discord.Member, amount: str):
        """Send money to someone"""
        if member.bot or member.id == ctx.author.id:
            return await ctx.send("Pick someone else.")
        mine = self.wallet(ctx.author.id)
        n = self.amount(amount, mine["cash"])
        if n < 1:
            return await ctx.send("Send at least 1.")
        if n > mine["cash"]:
            return await ctx.send(f"You only have {fmt(mine['cash'])} in cash.")
        mine["cash"] -= n
        self.wallet(member.id)["cash"] += n
        self.store.save()
        await ctx.send(f"💸 You sent {fmt(n)} to **{member.display_name}**.", allowed_mentions=NO_PINGS)

    @commands.command(aliases=["steal"])
    async def rob(self, ctx, member: discord.Member):
        """Try to steal some cash from someone"""
        if member.bot or member.id == ctx.author.id:
            return await ctx.send("Pick someone else.")
        me = self.wallet(ctx.author.id)
        left = me["rob"] + ROB_COOLDOWN - time.time()
        if left > 0:
            return await ctx.send(f"⏳ You can rob again <t:{int(time.time() + left)}:R>.")
        target = self.wallet(member.id)
        if target["cash"] < 100:
            return await ctx.send(f"**{member.display_name}** has less than {fmt(100)} in cash, not worth it.",
                                  allowed_mentions=NO_PINGS)
        me["rob"] = int(time.time())
        if random.random() < ROB_SUCCESS:
            loot = random.randint(max(1, target["cash"] // 10), max(1, target["cash"] // 3))
            target["cash"] -= loot
            me["cash"] += loot
            msg = f"🦹 You stole {fmt(loot)} from **{member.display_name}**!"
        else:
            fine = min(me["cash"], random.randint(50, 250))
            me["cash"] -= fine
            msg = f"🚓 You got caught and paid a {fmt(fine)} fine."
        self.store.save()
        await ctx.send(msg, allowed_mentions=NO_PINGS)

    @commands.command(aliases=["dep"])
    async def deposit(self, ctx, amount: str):
        """Move cash into your bank"""
        w = self.wallet(ctx.author.id)
        n = self.amount(amount, w["cash"])
        if n < 1 or n > w["cash"]:
            return await ctx.send(f"You have {fmt(w['cash'])} in cash.")
        w["cash"] -= n
        w["bank"] += n
        self.store.save()
        await ctx.send(f"🏦 Deposited {fmt(n)}.")

    @commands.command(aliases=["with"])
    async def withdraw(self, ctx, amount: str):
        """Take cash out of your bank"""
        w = self.wallet(ctx.author.id)
        n = self.amount(amount, w["bank"])
        if n < 1 or n > w["bank"]:
            return await ctx.send(f"You have {fmt(w['bank'])} in the bank.")
        w["bank"] -= n
        w["cash"] += n
        self.store.save()
        await ctx.send(f"🏦 Withdrew {fmt(n)}.")

    @commands.command(aliases=["richest", "lb"])
    async def baltop(self, ctx):
        """Richest users across all servers"""
        ranked = sorted(((u["cash"] + u["bank"], uid) for uid, u in self.users().items()
                         if u["cash"] + u["bank"] > 0), reverse=True)[:100]
        lines = []
        for i, (total, uid) in enumerate(ranked, 1):
            user = self.bot.get_user(int(uid))
            if user is None:
                try:
                    user = await self.bot.fetch_user(int(uid))
                except discord.HTTPException:
                    user = None
            name = discord.utils.escape_markdown(user.name) if user else f"Unknown user ({uid})"
            lines.append(f"`{i}.` {name} — {fmt(total)}")
        await paginate(ctx, chunk_pages("Richest users", lines, COLOR, per_page=10, empty="Nobody has any money yet."))

    # ---- games ---------------------------------------------------------------
    @commands.command(aliases=["bet"])
    async def gamble(self, ctx, amount: str):
        """Double it or lose it"""
        got = await self._bet(ctx, amount)
        if not got:
            return
        w, bet = got
        if random.random() < 0.47:
            w["cash"] += bet
            msg = f"🎉 You won! +{fmt(bet)}"
        else:
            w["cash"] -= bet
            msg = f"💀 You lost {fmt(bet)}."
        self.store.save()
        await ctx.send(f"{msg}\nCash: {fmt(w['cash'])}")

    @commands.command()
    async def slots(self, ctx, amount: str):
        """Spin the slot machine"""
        got = await self._bet(ctx, amount)
        if not got:
            return
        w, bet = got
        reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
        if reels[0] == reels[1] == reels[2]:
            gain, text = bet * 9, "JACKPOT!"
        elif len(set(reels)) == 2:
            gain, text = bet, "Two of a kind."
        else:
            gain, text = -bet, "No match."
        w["cash"] += gain
        self.store.save()
        result = f"+{fmt(gain)}" if gain > 0 else f"-{fmt(-gain)}"
        await ctx.send(f"🎰 | {' | '.join(reels)} |\n**{text}** {result}\nCash: {fmt(w['cash'])}")

    @commands.command(aliases=["cf"])
    async def coinflip(self, ctx, side: str, amount: str):
        """Call heads or tails, double it or lose it"""
        side = side.lower()
        side = {"h": "heads", "t": "tails"}.get(side, side)
        if side not in ("heads", "tails"):
            raise commands.BadArgument("side")
        got = await self._bet(ctx, amount)
        if not got:
            return
        w, bet = got
        result = random.choice(["heads", "tails"])
        if result == side:
            w["cash"] += bet
            msg = f"🪙 It landed **{result}**. You won +{fmt(bet)}!"
        else:
            w["cash"] -= bet
            msg = f"🪙 It landed **{result}**. You lost {fmt(bet)}."
        self.store.save()
        await ctx.send(f"{msg}\nCash: {fmt(w['cash'])}")

    # ---- admin ---------------------------------------------------------------
    @commands.group(invoke_without_command=True)
    @bot_owner_only()
    async def economy(self, ctx):
        """Manage members' money (bot owner only)"""
        await ctx.send(f"Use `{ctx.clean_prefix}economy add|remove|reset`.")

    @economy.command(name="add")
    @bot_owner_only()
    async def economy_add(self, ctx, member: discord.Member, amount: int):
        """Give a member money"""
        if amount < 1:
            return await ctx.send("Give a positive amount.")
        self.wallet(member.id)["cash"] += amount
        self.store.save()
        await ctx.send(f"✅ Gave {fmt(amount)} to **{member.display_name}**.", allowed_mentions=NO_PINGS)

    @economy.command(name="remove")
    @bot_owner_only()
    async def economy_remove(self, ctx, member: discord.Member, amount: int):
        """Take money from a member's cash"""
        w = self.wallet(member.id)
        w["cash"] = max(0, w["cash"] - max(0, amount))
        self.store.save()
        await ctx.send(f"✅ Took {fmt(max(0, amount))} from **{member.display_name}**.", allowed_mentions=NO_PINGS)

    @economy.command(name="reset")
    @bot_owner_only()
    async def economy_reset(self, ctx, member: discord.Member):
        """Wipe a member's money"""
        self.users().pop(str(member.id), None)
        self.store.save()
        await ctx.send(f"✅ Reset **{member.display_name}**'s money.", allowed_mentions=NO_PINGS)


async def setup(bot):
    add_cog_safely(bot, Economy(bot))
