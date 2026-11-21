"""Standalone vouch system. Drop into your cogs/ folder and load it:
    await bot.load_extension("cogs.vouch")
Needs: discord.py 2.6+ (for the new-style help panel; older versions fall back to an embed)
and the Server Members intent. Saves to vouch_data.json.

Your own help menu can reuse the panel:
    view = bot.get_cog("Vouch").help_view(ctx.author.id)
    await ctx.send(view=view)
"""
import json, os
import discord
from discord.ext import commands, tasks

PATH = "vouch_data.json"
DATA = {}
if os.path.exists(PATH):
    try:
        DATA = json.load(open(PATH))
    except Exception:
        DATA = {}

def save():
    json.dump(DATA, open(PATH + ".tmp", "w")); os.replace(PATH + ".tmp", PATH)

def cfg(gid):
    d = DATA.setdefault(str(gid), {})
    for k, v in {"role": None, "givers": [], "limit": 3, "limits": {}, "vouches": {}, "blacklist": [],
                 "crole": None, "community": {}, "backdoor": []}.items():
        d.setdefault(k, v)
    return d

POOLS = (("role", "vouches"), ("crole", "community"))

def holds(d, role_id, uid):
    """True if this member is vouched in ANY pool that uses this role (the vouch role and the
    community role can be the same shared role)."""
    return any(d[pool].get(str(uid)) for key, pool in POOLS if d[key] == role_id)

async def server_owner(ctx):
    if ctx.guild and (ctx.author.id == ctx.guild.owner_id or await ctx.bot.is_owner(ctx.author)): return True
    if ctx.guild: await ctx.send("Server owner only.")
    return False

LEVEL_CEO = 80  # same as the CEO level in cogs/rank_management.py

async def ceo_plus(ctx):
    """Bot owner, server owner, or anyone ranked CEO or higher by the Rank Management cog."""
    if not ctx.guild: return False
    if ctx.author.id == ctx.guild.owner_id or await ctx.bot.is_owner(ctx.author): return True
    ranks = ctx.bot.get_cog("Rank Management")
    if ranks is not None and ranks.level(ctx.guild, ctx.author.id) >= LEVEL_CEO: return True
    await ctx.send("CEO or higher only.")
    return False

def fool_plus(ctx):
    """Community vouch gate. Uses your rank system if you have util.rank_level, else needs Manage Server."""
    try:
        import util
        return util.rank_level(ctx.guild, ctx.author.id) >= util.LEVEL["fool"] or ctx.author.guild_permissions.administrator
    except Exception:
        return ctx.author.guild_permissions.manage_guild

# ====================================================================== help panel
SECTIONS = [
    ("Vouch Giver / Vouch Role", [
        ("-vouch @user", "Give a vouch"),
        ("-vouch give @user", "Give a vouch"),
        ("-vouch take @user", "Remove your latest vouch for a member"),
        ("-vouch list", "Paginated server vouches"),
        ("-vouch info @user", "Member vouch information"),
    ]),
    ("Community Vouch", [
        ("-cvouch", "View the Community Vouch system"),
        ("-cvouch @user", "Give a Community Vouch (Fool+)"),
        ("-cvouch take @user", "Remove a Community Vouch (Fool+)"),
        ("-cvouch list", "View Community Vouches (Fool+)"),
        ("-cvouch setrole @role", "Set the shared Vouch Role (Server Owner or Bot Owner)"),
    ]),
    ("Server Owner", [
        ("-vouch config", "View configuration"),
        ("-vouch setrole @role", "Set the vouch role"),
        ("-vouch unsetrole", "Remove the configured role"),
        ("-vouch addgiver @user", "Add a manual giver"),
        ("-vouch removegiver @user", "Remove a manual giver"),
        ("-vouch limit <number>", "Set the default give limit"),
        ("-vouch limit @user <number>", "Set a member's limit (0 resets it)"),
        ("-vouch wipeall", "Clear server vouches"),
        ("-vouch blacklist @user", "Blacklist a member"),
        ("-vouch unblacklist @user", "Remove the blacklist entry"),
        ("-vouch blacklist list", "View blacklisted members"),
    ]),
]

def section_text(title, items):
    return f"## {title}\n" + "\n".join(f"`{c}` - {d}" for c, d in items)

def plain_text():
    return "# Vouch System\n\n" + "\n\n".join(section_text(t, i) for t, i in SECTIONS)

if hasattr(discord.ui, "LayoutView"):          # discord.py 2.6+ (Components V2)
    class HelpView(discord.ui.LayoutView):
        def __init__(self, author_id=None):
            super().__init__(timeout=300)
            self.author_id = author_id
            select = discord.ui.Select(
                placeholder="Vouch System",
                options=[discord.SelectOption(label="Vouch System", value="vouch", default=True,
                                              description="Vouches, giver access, roles, and limits")])
            select.callback = self.on_select
            kids = [discord.ui.TextDisplay("# Vouch System")]
            for title, items in SECTIONS:
                kids.append(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large))
                kids.append(discord.ui.TextDisplay(section_text(title, items)))
            kids.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            kids.append(discord.ui.ActionRow(select))
            self.add_item(discord.ui.Container(*kids))

        async def on_select(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=self)

        async def interaction_check(self, interaction: discord.Interaction):
            if self.author_id and interaction.user.id != self.author_id:
                await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
                return False
            return True
else:
    HelpView = None

async def send_help(ctx):
    if HelpView:
        await ctx.send(view=HelpView(ctx.author.id))
    else:   # older discord.py: same text in an embed
        await ctx.send(embed=discord.Embed(description=plain_text(), color=0x0b0b0e))

# ====================================================================== pager for -vouch list
class Pager(discord.ui.View):
    def __init__(self, pages):
        super().__init__(timeout=300); self.pages, self.i = pages, 0
    def emb(self):
        return discord.Embed(title="Server vouches", description=self.pages[self.i], color=0x2b2d31).set_footer(text=f"Page {self.i+1}/{len(self.pages)}")
    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, it, b):
        self.i = (self.i - 1) % len(self.pages); await it.response.edit_message(embed=self.emb(), view=self)
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, it, b):
        self.i = (self.i + 1) % len(self.pages); await it.response.edit_message(embed=self.emb(), view=self)

# Page shown in the bot's main -help menu (general.py swaps {p} for the server's prefix).
HELP_SECTIONS = [
    ("Vouch Giver / Vouch Role", None, SECTIONS[0][1]),
    ("Community Vouch", "Fool rank or higher", SECTIONS[1][1]),
    ("Server Owner", "Server Owner", SECTIONS[2][1]),
    ("Backdoor List", "CEO or higher", [
        ("-backdoor add @user", "Add a member to the backdoor list"),
        ("-backdoor remove @user", "Remove a member from the backdoor list"),
        ("-backdoor list", "View everyone on the backdoor list"),
    ]),
]

def help_page():
    parts = ["# Vouch\n"]
    for title, required, items in HELP_SECTIONS:
        block = f"## {title}\n"
        if required:
            block += f"**Required:** {required}\n"
        block += "\n".join(f"`{{p}}{c.lstrip('-')}` - {d}" for c, d in items)
        parts.append(block)
    parts.append("If someone on the backdoor list is vouched for, the person who tried to vouch "
                 "them loses their roles and rank.")
    return "\n\n".join(parts)

# ====================================================================== cog
class Vouch(commands.Cog, description="Vouches, giver access, roles, and limits"):
    help_text = help_page()

    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.sweep_loop.cancel()

    def help_view(self, author_id=None):
        return HelpView(author_id) if HelpView else None

    async def sync_role(self, guild, member, key="role", pool="vouches"):
        d = cfg(guild.id); role = guild.get_role(d[key]) if d[key] else None
        if not role: return
        has = holds(d, role.id, member.id)
        try:
            if has and role not in member.roles: await member.add_roles(role)
            elif not has and role in member.roles: await member.remove_roles(role)
        except discord.HTTPException: pass

    def can_give(self, guild, m):
        d = cfg(guild.id); role = guild.get_role(d["role"]) if d["role"] else None
        return m.id in d["givers"] or m.id == guild.owner_id or (role is not None and role in m.roles)

    @staticmethod
    def given(d, uid):
        return sum(1 for l in d["vouches"].values() for v in l if v["by"] == uid)

    def reset_rank(self, gid, uid):
        """Sets the member's rank back to plain Member (works with the bot's rank system if present)."""
        try:
            import store
            for ids in store.g(gid, "ranks", {}).values():
                if uid in ids: ids.remove(uid)
            store.save()
        except Exception:
            pass

    async def backdoor_trap(self, ctx, member):
        """If the target is on the backdoor list, whoever tried to vouch them loses their roles and rank."""
        d = cfg(ctx.guild.id)
        if member.id not in d["backdoor"]: return False
        who = ctx.author
        if who.id != ctx.guild.owner_id and not await ctx.bot.is_owner(who):
            roles = [r for r in who.roles if r != ctx.guild.default_role and not r.managed and r < ctx.guild.me.top_role]
            if roles:
                try: await who.remove_roles(*roles, reason="Tried to vouch a backdoor-listed member")
                except discord.HTTPException: pass
            self.reset_rank(ctx.guild.id, who.id)
        await ctx.send("You can't vouch for that member.")
        return True

    # ---- role cleanup: anyone holding the vouch / community role without a vouch loses it ----
    async def sync_all(self, guild):
        """Remove the vouch / community role from anyone who has no vouch. Logs every result."""
        d = cfg(guild.id)
        if guild.chunked is False:
            await guild.chunk()          # make sure role.members is complete
        for rid in {d[k] for k, _ in POOLS if d[k]}:
            role = guild.get_role(rid)
            if not role:
                continue
            if role >= guild.me.top_role:
                print(f"[vouch] can't remove {role.name}: it's at or above the bot's top role")
                continue
            for m in role.members:
                if m.bot or holds(d, rid, m.id):
                    continue
                try:
                    await m.remove_roles(role, reason="Vouch role without a vouch")
                    print(f"[vouch] removed {role.name} from {m} (no vouch)")
                except discord.HTTPException as e:
                    print(f"[vouch] failed to remove {role.name} from {m}: {e}")

    @tasks.loop(minutes=1)
    async def sweep_loop(self):
        for guild in self.bot.guilds:
            await self.sync_all(guild)

    @sweep_loop.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    async def _role_remover(self, guild, member):
        """Who removed a role from this member, from the audit log (None if unknown)."""
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.member_role_update):
                if entry.target and entry.target.id == member.id and \
                        (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                    return entry.user
        except discord.HTTPException:
            pass
        return None

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        """Two-way sync between the vouch role(s) and the vouch data."""
        if after.bot:
            return
        d = cfg(after.guild.id)
        changed = False
        for rid in {d[k] for k, _ in POOLS if d[k]}:
            role = after.guild.get_role(rid)
            if not role:
                continue
            had, has = role in before.roles, role in after.roles
            vouched = holds(d, rid, after.id)
            if had and not has and vouched:
                remover = await self._role_remover(after.guild, after)
                if remover is None or remover.bot:
                    # a bot removed it (or we can't tell): put it back, keep the vouch
                    try:
                        await after.add_roles(role, reason="Vouched: restoring vouch role")
                        print(f"[vouch] restored {role.name} on {after} (removed by {remover or 'unknown'})")
                    except discord.HTTPException as e:
                        print(f"[vouch] failed to restore {role.name} on {after}: {e}")
                else:
                    # a person took the role away -> remove their vouches for this role
                    for key, pool in POOLS:
                        if d[key] == rid:
                            d[pool].pop(str(after.id), None)
                    changed = True
                    print(f"[vouch] {remover} removed {role.name} from {after}, removed their vouches")
            elif has and not had and not vouched:
                try:
                    await after.remove_roles(role, reason="Vouch role without a vouch")
                except discord.HTTPException as e:
                    print(f"[vouch] failed to remove {role.name} from {after}: {e}")
        if changed:
            save()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.sweep_loop.is_running():
            self.sweep_loop.start()

    # ------------------------------------------------ Vouch Giver / Vouch Role
    @commands.group(name="vouch", invoke_without_command=True, help="Give a vouch (or open the vouch help)", brief="Vouch system")
    @commands.guild_only()
    async def vouch(self, ctx, member: discord.Member = None):
        if member is None: return await send_help(ctx)
        await self.give(ctx, member)

    @vouch.command(name="help", help="Open the vouch help menu")
    async def v_help(self, ctx): await send_help(ctx)

    async def give(self, ctx, member):
        d = cfg(ctx.guild.id)
        if member.bot or member == ctx.author: return await ctx.send("You can't vouch for yourself or a bot.")
        if await self.backdoor_trap(ctx, member): return
        if ctx.author.id in d["blacklist"]: return await ctx.send("You're blacklisted from vouching.")
        if not self.can_give(ctx.guild, ctx.author): return await ctx.send("You aren't a vouch giver.")
        lst = d["vouches"].setdefault(str(member.id), [])
        if any(v["by"] == ctx.author.id for v in lst): return await ctx.send("You already vouched for them.")
        limit = d["limits"].get(str(ctx.author.id), d["limit"])
        if limit and self.given(d, ctx.author.id) >= limit:
            return await ctx.send(f"You've used all {limit} of your vouches. Use `-vouch take @user` to free one.")
        lst.append({"by": ctx.author.id}); save(); await self.sync_role(ctx.guild, member)
        await ctx.send(f"**{ctx.author.display_name}** vouched for **{member.display_name}** ({len(lst)} total).")

    @vouch.command(name="give", help="Give a vouch")
    async def v_give(self, ctx, member: discord.Member): await self.give(ctx, member)

    @vouch.command(name="take", help="Remove your latest vouch for a member")
    async def v_take(self, ctx, member: discord.Member):
        d = cfg(ctx.guild.id); lst = d["vouches"].get(str(member.id), [])
        mine = [v for v in lst if v["by"] == ctx.author.id]
        if not mine: return await ctx.send("You haven't vouched for them.")
        lst.remove(mine[-1])
        if not lst: d["vouches"].pop(str(member.id), None)
        save(); await self.sync_role(ctx.guild, member)
        await ctx.send(f"Removed your latest vouch for **{member.display_name}**.")

    @vouch.command(name="list", help="Paginated server vouches")
    async def v_list(self, ctx):
        d = cfg(ctx.guild.id)
        rows = [f"<@{u}> - **{len(v)}** vouch{'es' if len(v) != 1 else ''}" for u, v in sorted(d["vouches"].items(), key=lambda x: -len(x[1])) if v]
        if not rows: return await ctx.send("No vouches yet.")
        v = Pager(["\n".join(rows[i:i + 10]) for i in range(0, len(rows), 10)]); await ctx.send(embed=v.emb(), view=v)

    @vouch.command(name="info", help="Member vouch information")
    async def v_info(self, ctx, member: discord.Member):
        d = cfg(ctx.guild.id); lst = d["vouches"].get(str(member.id), []); giver = self.can_give(ctx.guild, member)
        lim = d["limits"].get(str(member.id), d["limit"])
        text = (f"Vouches received: **{len(lst)}**\nFrom: " + (" ".join(f"<@{v['by']}>" for v in lst[:15]) or "nobody") +
                f"\nCan give vouches: **{'yes' if giver else 'no'}**" + (f" ({self.given(d, member.id)}/{lim or 'unlimited'} used)" if giver else "") +
                f"\nBlacklisted: **{'yes' if member.id in d['blacklist'] else 'no'}**")
        await ctx.send(embed=discord.Embed(title=member.display_name, description=text, color=0x2b2d31))

    # ------------------------------------------------ Server Owner
    @vouch.command(name="config", help="View configuration")
    @commands.check(server_owner)
    async def v_config(self, ctx):
        d = cfg(ctx.guild.id)
        await ctx.send(embed=discord.Embed(title="Vouch configuration", color=0x2b2d31, description=
            f"Vouch role: {('<@&%s>' % d['role']) if d['role'] else 'none'}\nDefault give limit: **{d['limit']}**\n"
            f"Manual givers: {' '.join(f'<@{i}>' for i in d['givers']) or 'none'}\nCustom limits: {len(d['limits'])}\n"
            f"Blacklisted: {len(d['blacklist'])}\nCommunity vouch role: {('<@&%s>' % d['crole']) if d['crole'] else 'none'}"))

    @vouch.command(name="setrole", help="Set the vouch role")
    @commands.check(server_owner)
    async def v_setrole(self, ctx, role: discord.Role):
        cfg(ctx.guild.id)["role"] = role.id
        save()
        await self.sync_all(ctx.guild)
        await ctx.send(f"Vouch role set to {role.mention}.")

    @vouch.command(name="unsetrole", help="Remove the configured role")
    @commands.check(server_owner)
    async def v_unsetrole(self, ctx):
        cfg(ctx.guild.id)["role"] = None; save(); await ctx.send("Vouch role removed.")

    @vouch.command(name="addgiver", help="Add a manual giver")
    @commands.check(server_owner)
    async def v_addgiver(self, ctx, member: discord.Member):
        g = cfg(ctx.guild.id)["givers"]
        if member.id not in g: g.append(member.id)
        save(); await ctx.send(f"**{member.display_name}** can now give vouches.")

    @vouch.command(name="removegiver", help="Remove a manual giver")
    @commands.check(server_owner)
    async def v_removegiver(self, ctx, member: discord.Member):
        g = cfg(ctx.guild.id)["givers"]
        if member.id in g: g.remove(member.id)
        save(); await ctx.send("Manual giver removed.")

    @vouch.command(name="limit", help="Set the default give limit, or a member's limit (0 resets it)")
    @commands.check(server_owner)
    async def v_limit(self, ctx, first: str, number: int = None):
        d = cfg(ctx.guild.id)
        if number is None:
            if not first.isdigit(): return await ctx.send("Usage: `-vouch limit <number>` or `-vouch limit @user <number>`")
            d["limit"] = int(first); save(); return await ctx.send(f"Default give limit set to **{first}**.")
        m = await commands.MemberConverter().convert(ctx, first)
        if number <= 0: d["limits"].pop(str(m.id), None); msg = f"**{m.display_name}**'s limit reset to the default."
        else: d["limits"][str(m.id)] = number; msg = f"**{m.display_name}**'s limit set to **{number}**."
        save(); await ctx.send(msg)

    @vouch.command(name="wipeall", help="Clear server vouches")
    @commands.check(server_owner)
    async def v_wipe(self, ctx):
        cfg(ctx.guild.id)["vouches"].clear()
        save()
        await self.sync_all(ctx.guild)
        await ctx.send("All server vouches cleared.")

    @vouch.group(name="blacklist", invoke_without_command=True, help="Blacklist a member")
    @commands.check(server_owner)
    async def v_blacklist(self, ctx, member: discord.Member):
        b = cfg(ctx.guild.id)["blacklist"]
        if member.id not in b: b.append(member.id)
        save(); await ctx.send(f"**{member.display_name}** is blacklisted from vouching.")

    @v_blacklist.command(name="list", help="View blacklisted members")
    @commands.check(server_owner)
    async def v_bl_list(self, ctx):
        await ctx.send("Blacklisted: " + (" ".join(f"<@{i}>" for i in cfg(ctx.guild.id)["blacklist"]) or "none"))

    @vouch.command(name="unblacklist", help="Remove the blacklist entry")
    @commands.check(server_owner)
    async def v_unbl(self, ctx, member: discord.Member):
        b = cfg(ctx.guild.id)["blacklist"]
        if member.id in b: b.remove(member.id)
        save(); await ctx.send("Removed from the blacklist.")

    # ------------------------------------------------ Community Vouch (Fool+)
    @commands.group(name="cvouch", invoke_without_command=True, help="View the Community Vouch system, or give a Community Vouch (Fool+)")
    @commands.guild_only()
    async def cvouch(self, ctx, member: discord.Member = None):
        if member is None: return await send_help(ctx)
        d = cfg(ctx.guild.id)
        if member.bot or member == ctx.author: return await ctx.send("You can't vouch for yourself or a bot.")
        if await self.backdoor_trap(ctx, member): return
        if not fool_plus(ctx): return await ctx.send("You need the **Fool** rank or higher.")
        lst = d["community"].setdefault(str(member.id), [])
        if ctx.author.id in lst: return await ctx.send("You already gave them a Community Vouch.")
        lst.append(ctx.author.id); save(); await self.sync_role(ctx.guild, member, "crole", "community")
        await ctx.send(f"Community Vouch given to **{member.display_name}** ({len(lst)} total).")

    @cvouch.command(name="take", help="Remove a Community Vouch (Fool+)")
    async def cv_take(self, ctx, member: discord.Member):
        if not fool_plus(ctx): return await ctx.send("You need the **Fool** rank or higher.")
        d = cfg(ctx.guild.id); lst = d["community"].get(str(member.id), [])
        if ctx.author.id not in lst: return await ctx.send("You haven't given them a Community Vouch.")
        lst.remove(ctx.author.id)
        if not lst: d["community"].pop(str(member.id), None)
        save(); await self.sync_role(ctx.guild, member, "crole", "community"); await ctx.send("Community Vouch removed.")

    @cvouch.command(name="list", help="View Community Vouches (Fool+)")
    async def cv_list(self, ctx):
        if not fool_plus(ctx): return await ctx.send("You need the **Fool** rank or higher.")
        c = cfg(ctx.guild.id)["community"]
        rows = [f"<@{u}> - **{len(v)}**" for u, v in sorted(c.items(), key=lambda x: -len(x[1]))[:25]]
        await ctx.send(embed=discord.Embed(title="Community Vouches", description="\n".join(rows) or "None yet.", color=0x2b2d31))

    @cvouch.command(name="setrole", help="Set the shared Vouch Role (Server Owner or Bot Owner)")
    @commands.check(server_owner)
    async def cv_setrole(self, ctx, role: discord.Role):
        cfg(ctx.guild.id)["crole"] = role.id
        save()
        await self.sync_all(ctx.guild)
        await ctx.send(f"Shared Community Vouch role set to {role.mention}.")

    # ------------------------------------------------ Backdoor list (hidden from the help panel)
    @commands.group(name="backdoor", invoke_without_command=True, hidden=True)
    @commands.guild_only()
    @commands.check(ceo_plus)
    async def backdoor(self, ctx):
        await ctx.send("`-backdoor add @user`, `-backdoor remove @user`, `-backdoor list`")

    @backdoor.command(name="add", hidden=True)
    @commands.check(ceo_plus)
    async def bd_add(self, ctx, member: discord.Member):
        b = cfg(ctx.guild.id)["backdoor"]
        if member.id not in b: b.append(member.id)
        save(); await ctx.send(f"**{member.display_name}** added to the backdoor list.")

    @backdoor.command(name="remove", hidden=True)
    @commands.check(ceo_plus)
    async def bd_remove(self, ctx, member: discord.Member):
        b = cfg(ctx.guild.id)["backdoor"]
        if member.id in b: b.remove(member.id)
        save(); await ctx.send(f"**{member.display_name}** removed from the backdoor list.")

    @backdoor.command(name="list", hidden=True)
    @commands.check(ceo_plus)
    async def bd_list(self, ctx):
        await ctx.send("Backdoor list: " + (" ".join(f"<@{i}>" for i in cfg(ctx.guild.id)["backdoor"]) or "empty"))

async def setup(bot):
    await bot.add_cog(Vouch(bot))