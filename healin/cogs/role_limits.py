import discord
from discord.ext import commands

from utils.store import GuildStore

COLOR = 0x0F0F10
NO_PINGS = discord.AllowedMentions.none()

DEFAULTS = {
    "roles": {},  # role id (as str) -> max number of members who can hold it
    "max": None,  # max roles a single member can hold (None = off)
}


def is_admin():
    """Server owner or an Anti-Nuke admin."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        if ctx.author.id == ctx.guild.owner_id:
            return True
        antinuke = ctx.bot.get_cog("AntiNuke")
        if antinuke and ctx.author.id in antinuke.cfg(ctx.guild)["admins"]:
            return True
        raise commands.CheckFailure("Only the server owner or an Anti-Nuke admin can use this command.")

    return commands.check(predicate)


def counted_roles(member: discord.Member) -> list:
    """Roles that count towards the per-member max (not @everyone, not bot/integration roles)."""
    return [r for r in member.roles if not r.is_default() and not r.managed]


class RoleLimits(commands.Cog, name="Role Limits"):
    """Member and per-role limit commands"""

    help_text = """# Role Limits

**Required:** Anti-Nuke Admin


`{p}rlimit` - View role-limit settings
`{p}rlimit @role <number>` - Set a role cap
`{p}rlimit max <1-100>` - Set maximum roles per member
`{p}rlimit remove @role` - Remove a role cap
`{p}rlimit scan` - Remove roles from members who break a limit
`{p}clear rlimit` - Clear all configured role limits"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = GuildStore("role_limits", DEFAULTS)

    def cfg(self, guild: discord.Guild) -> dict:
        return self.store.get(guild.id)

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error) or "You can't use that command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}"
            await ctx.send(f"Usage: `{usage}`")
        else:
            raise error

    async def log(self, guild: discord.Guild, text: str):
        """Post to the Anti-Nuke log channel if one is set."""
        antinuke = self.bot.get_cog("AntiNuke")
        if antinuke is None:
            return
        channel_id = antinuke.cfg(guild)["logchannel"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                await channel.send(text, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass

    # -------------------------------------------------------------- enforcement

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return
        guild = after.guild
        cfg = self.cfg(guild)
        if not cfg["roles"] and not cfg["max"]:
            return

        added = [r for r in after.roles if r not in before.roles]
        if not added:
            return

        remove = []
        reasons = []

        # per-role caps
        for role in added:
            cap = cfg["roles"].get(str(role.id))
            if cap is not None and len(role.members) > cap:
                remove.append(role)
                reasons.append(f"{role.name} is capped at {cap} members")

        # per-member max
        limit = cfg["max"]
        if limit:
            remaining = [r for r in counted_roles(after) if r not in remove]
            excess = len(remaining) - limit
            if excess > 0:
                candidates = [r for r in added if not r.managed and r not in remove]
                candidates.sort(key=lambda r: r.position, reverse=True)
                chosen = candidates[:excess]
                remove.extend(chosen)
                if chosen:
                    reasons.append(f"members can hold at most {limit} roles")

        if not remove:
            return

        names = ", ".join(r.name for r in remove)
        try:
            await after.remove_roles(*remove, reason=f"Role limits: {'; '.join(reasons)}")
            await self.log(guild, f"Role limit: removed **{names}** from {after.mention} ({'; '.join(reasons)}).")
        except discord.HTTPException:
            await self.log(
                guild,
                f"Role limit: couldn't remove **{names}** from {after.mention}. "
                "Check the bot's Manage Roles permission and role position.",
            )

    # ----------------------------------------------------------------- commands

    async def _send_settings(self, ctx: commands.Context):
        guild = ctx.guild
        cfg = self.cfg(guild)

        lines = []
        for rid, cap in cfg["roles"].items():
            role = guild.get_role(int(rid))
            if role:
                lines.append(f"{role.mention} - {len(role.members)}/{cap} members")
            else:
                lines.append(f"Deleted role `{rid}` - cap {cap}")

        embed = discord.Embed(title="Role limits", color=COLOR)
        embed.add_field(
            name="Max roles per member",
            value=str(cfg["max"]) if cfg["max"] else "Off",
            inline=False,
        )
        embed.add_field(name="Role caps", value="\n".join(lines)[:1024] or "None", inline=False)
        if not self.bot.intents.members:
            embed.set_footer(
                text="Server Members intent is off in main.py, so limits can't be enforced yet."
            )
        await ctx.send(embed=embed)

    @commands.group(name="rlimit", invoke_without_command=True)
    @is_admin()
    async def rlimit(
        self,
        ctx: commands.Context,
        role: discord.Role = None,
        number: commands.Range[int, 1, 100000] = None,
    ):
        """View role-limit settings, or set a cap with: rlimit @role <number>"""
        if role is None:
            return await self._send_settings(ctx)

        cfg = self.cfg(ctx.guild)
        if number is None:
            cap = cfg["roles"].get(str(role.id))
            text = (
                f"**{role.name}** is capped at **{cap}** members."
                if cap is not None
                else f"**{role.name}** has no cap. Usage: `{ctx.prefix}rlimit @role <number>`"
            )
            return await ctx.send(text, allowed_mentions=NO_PINGS)

        if role.is_default() or role.managed:
            return await ctx.send("You can't cap @everyone or bot/integration roles.")

        cfg["roles"][str(role.id)] = number
        self.store.save()

        text = f"**{role.name}** can now be held by at most **{number}** members."
        current = len(role.members)
        if current > number:
            text += f" It has {current} right now, so run `{ctx.prefix}rlimit scan` to remove it from the extras."
        await ctx.send(text, allowed_mentions=NO_PINGS)

    @rlimit.command(name="max")
    @is_admin()
    async def rlimit_max(self, ctx: commands.Context, amount: commands.Range[int, 1, 100]):
        """Set maximum roles per member"""
        cfg = self.cfg(ctx.guild)
        cfg["max"] = amount
        self.store.save()
        await ctx.send(
            f"Members can now hold at most **{amount}** roles (not counting @everyone and bot roles)."
        )

    @rlimit.command(name="remove")
    @is_admin()
    async def rlimit_remove(self, ctx: commands.Context, role: discord.Role):
        """Remove a role cap"""
        cfg = self.cfg(ctx.guild)
        if str(role.id) not in cfg["roles"]:
            return await ctx.send(f"**{role.name}** has no cap.", allowed_mentions=NO_PINGS)
        del cfg["roles"][str(role.id)]
        self.store.save()
        await ctx.send(f"Removed the cap on **{role.name}**.", allowed_mentions=NO_PINGS)

    def _is_protected(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Server owner and Anti-Nuke admins are the last to lose a capped role."""
        if member.id == guild.owner_id:
            return True
        antinuke = self.bot.get_cog("AntiNuke")
        return bool(antinuke and member.id in antinuke.cfg(guild)["admins"])

    @rlimit.command(name="scan")
    @is_admin()
    async def rlimit_scan(self, ctx: commands.Context):
        """Find members who break a limit and remove the roles automatically"""
        if not self.bot.intents.members:
            return await ctx.send(
                "I can't scan without the Server Members intent. Turn it on in the developer portal "
                "and add `intents.members = True` in `main.py`."
            )

        guild = ctx.guild
        cfg = self.cfg(guild)
        plan = {}  # member -> {role: reason}

        # per-role caps: the server owner and Anti-Nuke admins keep the role first,
        # then the members who joined earliest; everyone past the cap loses it
        for rid, cap in cfg["roles"].items():
            role = guild.get_role(int(rid))
            if role is None or len(role.members) <= cap:
                continue
            holders = sorted(
                role.members,
                key=lambda m: (
                    not self._is_protected(guild, m),
                    m.joined_at.timestamp() if m.joined_at else 0,
                ),
            )
            for member in holders[cap:]:
                plan.setdefault(member, {})[role] = f"{role.name} is capped at {cap} members"

        # per-member max: roles beyond the limit are removed, highest position first
        # (same order the live enforcement uses)
        limit = cfg["max"]
        if limit:
            for member in guild.members:
                if member.id == guild.owner_id:
                    continue
                dropped = plan.get(member, {})
                remaining = [r for r in counted_roles(member) if r not in dropped]
                excess = len(remaining) - limit
                if excess > 0:
                    remaining.sort(key=lambda r: r.position, reverse=True)
                    for role in remaining[:excess]:
                        plan.setdefault(member, {})[role] = f"members can hold at most {limit} roles"

        if not plan:
            embed = discord.Embed(title="Role limit scan", description="No violations found.", color=COLOR)
            return await ctx.send(embed=embed)

        removed = []
        failed = []
        async with ctx.typing():
            for member, roles in plan.items():
                too_high = [r for r in roles if r >= guild.me.top_role]
                doable = [r for r in roles if r not in too_high]
                if doable:
                    try:
                        await member.remove_roles(
                            *doable,
                            reason=f"Role limits scan by {ctx.author}: {'; '.join(set(roles.values()))}",
                        )
                        removed.append(f"{member.mention}: {', '.join(r.name for r in doable)}")
                    except discord.HTTPException:
                        too_high.extend(doable)
                if too_high:
                    failed.append(f"{member.mention}: {', '.join(r.name for r in too_high)}")

        embed = discord.Embed(title="Role limit scan", color=COLOR)
        embed.description = (
            f"Removed roles from **{len(removed)}** member(s)."
            + (f" Couldn't fix **{len(failed)}**." if failed else "")
        )
        if removed:
            embed.add_field(name="Removed", value="\n".join(removed)[:1024], inline=False)
        if failed:
            embed.add_field(
                name="Couldn't remove (role above the bot or missing permission)",
                value="\n".join(failed)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)
        await self.log(
            guild,
            f"Role limit scan by {ctx.author.mention}: removed roles from {len(removed)} member(s)"
            + (f", {len(failed)} couldn't be fixed." if failed else "."),
        )

    @commands.group(name="clear", invoke_without_command=True)
    @is_admin()
    async def clear_group(self, ctx: commands.Context):
        """Clear settings"""
        await ctx.send(f"Usage: `{ctx.prefix}clear rlimit`")

    @clear_group.command(name="rlimit")
    @is_admin()
    async def clear_rlimit(self, ctx: commands.Context):
        """Clear all configured role limits"""
        cfg = self.cfg(ctx.guild)
        cfg["roles"] = {}
        cfg["max"] = None
        self.store.save()
        await ctx.send("Cleared all role limits.")


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleLimits(bot))
