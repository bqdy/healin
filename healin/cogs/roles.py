import json
import pathlib
import re

import discord
from discord.ext import commands

ROLE_RE = re.compile(r"^<@&(\d+)>$|^(\d{17,20})$")
MEMBER_RE = re.compile(r"^(<@!?\d+>|\d{17,20})\s*(.*)$", re.S)
LIST_LIMIT = 50
STORE_PATH = pathlib.Path("data/roles_store.json")

HELP = {
    "r": "Toggle roles: -r @user <role>, -r <role>, -r @user <role 1>, <role 2>",
    "icon": "Set a Discord role icon: -r icon <emoji> <role>",
    "inrole": "List members with a role",
    "role": "Role management (create, rename, delete, info, members, color, all, removeall, humans, bots, restore)",
    "role create": "Create a role",
    "role rename": "Rename a role",
    "role delete": "Delete a role",
    "role info": "View role details",
    "role members": "View role members",
    "role all": "Add a role for everyone",
    "role removeall": "Remove a role from everyone",
    "role color": "Set a solid role colour, or -role color gradient <first> <second> <role>",
    "role humans": "Give a role to human members",
    "role bots": "Add/remove a role for bots",
    "role restore": "Restore roles saved when a member left",
    "roleinfo": "View role details; defaults to your top role",
    "roles": "List server roles",
    "rrs": "Restore roles saved when a member left",
    "boosterrole": "View booster-role settings",
    "boosterrole create": "Create a personal booster role",
    "boosterrole base": "Set or clear the base booster role",
    "boosterrole limit": "Set award roles per booster (1-25)",
    "boosterrole list": "View the full booster-role setup",
    "boosterrole rename": "Rename a booster role",
    "boosterrole remove": "Remove a booster role",
    "boosterrole cleanup": "Remove deleted award roles from the setup",
}


def load_store():
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text())
    return {"restore": {}, "booster": {}}


def save_store(store):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(store, indent=2))


def find_role(guild, query):
    query = query.strip()
    if not query:
        return None
    m = ROLE_RE.match(query)
    if m:
        return guild.get_role(int(m.group(1) or m.group(2)))
    q = query.lower()
    exact = [r for r in guild.roles if r.name.lower() == q]
    if exact:
        return exact[0]
    partial = [r for r in guild.roles if q in r.name.lower() and not r.is_default()]
    if partial:
        return min(partial, key=lambda r: len(r.name))
    return None


def split_role_and_rest(guild, text):
    """Finds the role at the start of text and returns (role, remaining text)."""
    words = text.split()
    for i in range(len(words), 0, -1):
        role = find_role(guild, " ".join(words[:i]))
        if role:
            return role, " ".join(words[i:]).strip()
    return None, text.strip()


def parse_color(text):
    try:
        return discord.Colour.from_str(text)
    except ValueError:
        fn = getattr(discord.Colour, text.lower().replace(" ", "_"), None)
        return fn() if callable(fn) else None


def can_manage(ctx, role):
    if role.is_default():
        return "You can't change @everyone."
    if role >= ctx.guild.me.top_role:
        return f"I can't manage **{role.name}** because it's above my highest role."
    if ctx.author != ctx.guild.owner and role >= ctx.author.top_role:
        return f"**{role.name}** is above your highest role."
    return None


async def split_member(ctx, text):
    m = MEMBER_RE.match(text.strip())
    if m:
        member = await commands.MemberConverter().convert(ctx, m.group(1))
        return member, m.group(2).strip()
    return ctx.author, text.strip()

ROLES_PER_PAGE = 10


class RolesPager(discord.ui.View):
    """Paginated -roles list: 10 per page, highest role first, numbered across pages."""

    def __init__(self, ctx, roles):
        super().__init__(timeout=300)
        self.ctx, self.roles, self.i = ctx, roles, 0
        self.pages = max(1, -(-len(roles) // ROLES_PER_PAGE))
        self.message = None

    def embed(self):
        start = self.i * ROLES_PER_PAGE
        chunk = self.roles[start:start + ROLES_PER_PAGE]
        lines = [f"`{start + n}.` {r.mention} (`{r.id}`)" for n, r in enumerate(chunk, 1)]
        e = discord.Embed(
            title=f"Roles in {self.ctx.guild.name}",
            description="\n".join(lines) or "No roles.",
            color=0x5865F2,
        )
        e.set_author(name=self.ctx.author.display_name, icon_url=self.ctx.author.display_avatar.url)
        e.set_footer(text=f"Page {self.i + 1} of {self.pages} ({len(self.roles)} roles)")
        return e

    async def interaction_check(self, it):
        if it.user.id != self.ctx.author.id:
            await it.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, it, button):
        self.i = (self.i - 1) % self.pages
        await it.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, it, button):
        self.i = (self.i + 1) % self.pages
        await it.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class RoleCog(commands.Cog, name="Roles"):
    """Role management, role restore and booster roles"""

    # general.py shows this page as-is; {p} becomes the bot's prefix
    help_text = (
        "# Roles\n\n"
        "**Required:** Manage Roles\n\n"
        "## Role Management\n\n"
        "`{p}r @user <role>` - Add or remove a role from a member\n"
        "`{p}r @user <role ID>` - Toggle a role by ID\n"
        "`{p}r @user <role name>` - Toggle the closest matching role name\n"
        "`{p}r @user <role 1>, <role 2>` - Toggle multiple roles at once\n"
        "`{p}r <role>` - Toggle one of your own roles\n"
        "`{p}role create <name>` - Create a role\n"
        "`{p}role rename <role> <name>` - Rename a role\n"
        "`{p}role delete <role>` - Delete a role\n"
        "`{p}role info <role>` / `{p}role members <role>` - View role details or members\n"
        "`{p}roleinfo [role]` / `{p}ri [role]` - View role details; defaults to your top role\n"
        "`{p}role all <role>` / `{p}role removeall <role>` - Add or remove a role for everyone\n"
        "`{p}role color <color> <role>` - Set a solid role colour\n"
        "`{p}role color gradient <first> <second> <role>` - Set a role gradient\n"
        "`{p}r icon <emoji> <role>` - Set a Discord role icon\n"
        "`{p}inrole <role mention/ID/name>` - List members with a role\n"
        "`{p}role humans <role mention/ID/name>` - Give a role to human members\n"
        "`{p}role restore @user` / `{p}rrs @user` - Restore roles saved when a member left\n"
        "`{p}role bots <role> [remove]` - Add/remove a role for bots\n"
        "`{p}roles` - List server roles\n\n"
        "## Booster Roles\n\n"
        "`{p}boosterrole` / `{p}br` - View booster-role settings\n"
        "`{p}boosterrole create` - Create a personal booster role\n"
        "`{p}boosterrole base [@role]` - Set or clear the base booster role\n"
        "`{p}boosterrole limit <1-25>` - Set award roles per booster\n"
        "`{p}boosterrole list` - View the full booster-role setup\n"
        "`{p}boosterrole rename <role> <name>` - Rename a booster role\n"
        "`{p}boosterrole remove @user <role>` - Remove a booster role\n"
        "`{p}boosterrole cleanup` - Remove deleted award roles from the setup"
    )

    def __init__(self, bot):
        self.bot = bot
        for cmd in self.walk_commands():
            if cmd.qualified_name in HELP:
                cmd.help = HELP[cmd.qualified_name]

    # Restore support: save roles when someone leaves

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if member.bot:
            return
        store = load_store()
        saved = [r.id for r in member.roles if not r.is_default() and not r.managed]
        store["restore"].setdefault(str(member.guild.id), {})[str(member.id)] = saved
        save_store(store)

    # -r @user <role>, -r <role>, -r <role 1>, <role 2>, -r icon <emoji> <role>

    @commands.command(name="r")
    @commands.guild_only()
    async def r(self, ctx, *, text: str):
        if text.lower().startswith("icon "):
            if not ctx.author.guild_permissions.manage_roles:
                return await ctx.send("You need Manage Roles to do that.")
            return await ctx.invoke(self.r_icon, text=text[5:])

        member, rest = await split_member(ctx, text)
        if member != ctx.author and not ctx.author.guild_permissions.manage_roles:
            return await ctx.send("You need Manage Roles to change someone else's roles.")
        if not rest:
            return await ctx.send("Usage: `-r @user <role>` or `-r <role>`")

        lines = []
        for name in (n for n in rest.split(",") if n.strip()):
            role = find_role(ctx.guild, name)
            if not role:
                lines.append(f"Role not found: {name.strip()}")
                continue
            if member == ctx.author and not ctx.author.guild_permissions.manage_roles and (
                role.permissions.manage_roles or role.permissions.administrator
            ):
                lines.append(f"You can't self-assign **{role.name}**.")
                continue
            if err := can_manage(ctx, role):
                lines.append(err)
                continue
            try:
                if role in member.roles:
                    await member.remove_roles(role, reason=f"Toggled by {ctx.author}")
                    lines.append(f"Removed **{role.name}** from {member.display_name}")
                else:
                    await member.add_roles(role, reason=f"Toggled by {ctx.author}")
                    lines.append(f"Added **{role.name}** to {member.display_name}")
            except discord.HTTPException:
                lines.append(f"Discord refused the change to **{role.name}**.")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    # -icon <emoji> <role>  (also reachable as -r icon <emoji> <role>)

    @commands.command(name="icon")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def r_icon(self, ctx, *, text: str):
        emoji_text, _, role_query = text.strip().partition(" ")
        role = find_role(ctx.guild, role_query)
        if not role:
            return await ctx.send("Usage: `-r icon <emoji> <role>`")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        try:
            pe = discord.PartialEmoji.from_str(emoji_text)
            if pe.is_custom_emoji():
                await role.edit(display_icon=await pe.read(), reason=f"Icon set by {ctx.author}")
            else:
                await role.edit(display_icon=emoji_text, reason=f"Icon set by {ctx.author}")
            await ctx.send(f"Set the icon of **{role.name}**.")
        except discord.HTTPException:
            await ctx.send("Discord refused. Role icons need server boost level 2.")

    # -role <subcommand>

    @commands.group(name="role", invoke_without_command=True)
    @commands.guild_only()
    async def role(self, ctx):
        await ctx.send(
            "Subcommands: `create`, `rename`, `delete`, `info`, `members`, `color`, `all`, "
            "`removeall`, `humans`, `bots`, `restore`"
        )

    @role.command(name="create")
    @commands.has_permissions(manage_roles=True)
    async def role_create(self, ctx, *, name: str):
        new_role = await ctx.guild.create_role(name=name, reason=f"Created by {ctx.author}")
        await ctx.send(f"Created role **{new_role.name}**.")

    @role.command(name="rename")
    @commands.has_permissions(manage_roles=True)
    async def role_rename(self, ctx, *, text: str):
        role, new_name = split_role_and_rest(ctx.guild, text)
        if not role or not new_name:
            return await ctx.send("Usage: `-role rename <role> <new name>`")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        old = role.name
        await role.edit(name=new_name, reason=f"Renamed by {ctx.author}")
        await ctx.send(f"Renamed **{old}** to **{new_name}**.")

    @role.command(name="delete")
    @commands.has_permissions(manage_roles=True)
    async def role_delete(self, ctx, *, query: str):
        role = find_role(ctx.guild, query)
        if not role:
            return await ctx.send("Role not found.")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        name = role.name
        await role.delete(reason=f"Deleted by {ctx.author}")
        await ctx.send(f"Deleted role **{name}**.")

    @role.command(name="info")
    async def role_info(self, ctx, *, query: str = ""):
        await self.send_role_info(ctx, query)

    @role.command(name="members")
    async def role_members(self, ctx, *, query: str):
        await self.send_in_role(ctx, query)

    @role.command(name="color", aliases=["colour"])
    @commands.has_permissions(manage_roles=True)
    async def role_color(self, ctx, *, text: str):
        # -role color <color> <role>  or  -role color gradient <first> <second> <role>
        parts = text.split(maxsplit=1)
        if parts and parts[0].lower() == "gradient":
            return await self.role_gradient(ctx, parts[1] if len(parts) > 1 else "")

        color_text, _, role_query = text.partition(" ")
        color = parse_color(color_text)
        role = find_role(ctx.guild, role_query)
        if not color or not role:
            return await ctx.send("Usage: `-role color <#hex or name> <role>`")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        await role.edit(colour=color, reason=f"Color set by {ctx.author}")
        await ctx.send(f"Set the color of **{role.name}**.")

    async def role_gradient(self, ctx, text):
        tokens = text.split(maxsplit=2)
        if len(tokens) < 3:
            return await ctx.send("Usage: `-role color gradient <first> <second> <role>`")
        first, second = parse_color(tokens[0]), parse_color(tokens[1])
        role = find_role(ctx.guild, tokens[2])
        if not first or not second or not role:
            return await ctx.send("Usage: `-role color gradient <first> <second> <role>`")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        try:
            # Gradients aren't in discord.py yet, so this calls the Discord API directly
            route = discord.http.Route(
                "PATCH", "/guilds/{guild_id}/roles/{role_id}",
                guild_id=ctx.guild.id, role_id=role.id,
            )
            await self.bot.http.request(route, json={
                "colors": {"primary_color": first.value, "secondary_color": second.value}
            })
            await ctx.send(f"Set a gradient on **{role.name}**.")
        except discord.HTTPException:
            await ctx.send("Discord refused the gradient. Gradients need server boost level 2.")

    @role.command(name="all")
    @commands.has_permissions(manage_roles=True)
    async def role_all(self, ctx, *, query: str):
        await self.bulk_change(ctx, query, add=True)

    @role.command(name="removeall")
    @commands.has_permissions(manage_roles=True)
    async def role_removeall(self, ctx, *, query: str):
        await self.bulk_change(ctx, query, add=False)

    @role.command(name="humans")
    @commands.has_permissions(manage_roles=True)
    async def role_humans(self, ctx, *, query: str):
        await self.bulk_change(ctx, query, add=True, humans_only=True)

    @role.command(name="bots")
    @commands.has_permissions(manage_roles=True)
    async def role_bots(self, ctx, *, text: str):
        text = text.strip()
        remove = text.lower().endswith("remove")
        query = text[: -len("remove")] if remove else text
        role = find_role(ctx.guild, query)
        if not role:
            return await ctx.send("Role not found.")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        targets = [m for m in ctx.guild.members if m.bot]
        await self.apply(ctx, role, targets, add=not remove)

    @role.command(name="restore")
    @commands.has_permissions(manage_roles=True)
    async def role_restore(self, ctx, *, text: str):
        member, _ = await split_member(ctx, text)
        await self.restore_roles(ctx, member)

    # -rrs @user (same as -role restore)

    @commands.command(name="rrs")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def rrs(self, ctx, *, text: str):
        member, _ = await split_member(ctx, text)
        await self.restore_roles(ctx, member)

    async def restore_roles(self, ctx, member):
        store = load_store()
        saved = store["restore"].get(str(ctx.guild.id), {}).get(str(member.id), [])
        if not saved:
            return await ctx.send(f"No saved roles for {member.display_name}.")
        restored = []
        for rid in saved:
            role = ctx.guild.get_role(rid)
            if role and role not in member.roles and not can_manage(ctx, role):
                try:
                    await member.add_roles(role, reason=f"Restored by {ctx.author}")
                    restored.append(role.name)
                except discord.HTTPException:
                    continue
        await ctx.send(
            f"Restored {len(restored)} role(s) to {member.display_name}: {', '.join(restored) or 'none'}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -roleinfo / -ri

    @commands.command(name="roleinfo", aliases=["ri"])
    @commands.guild_only()
    async def roleinfo(self, ctx, *, query: str = ""):
        await self.send_role_info(ctx, query)

    # -inrole <role>

    @commands.command(name="inrole")
    @commands.guild_only()
    async def inrole(self, ctx, *, query: str):
        await self.send_in_role(ctx, query)

    # -roles

    @commands.command(name="roles")
    @commands.guild_only()
    async def roles(self, ctx):
        # Highest role first, same order as the server's role list
        roles = [r for r in sorted(ctx.guild.roles, key=lambda r: -r.position) if not r.is_default()]
        if not roles:
            return await ctx.send("This server has no roles.")
        view = RolesPager(ctx, roles)
        if view.pages == 1:
            return await ctx.send(embed=view.embed(), allowed_mentions=discord.AllowedMentions.none())
        view.message = await ctx.send(
            embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    # Booster roles
    # Assumption: each booster gets one personal role, placed just above the base role.
    # "limit" is the most personal booster roles the server allows per booster.

    @commands.group(name="boosterrole", aliases=["br"], invoke_without_command=True)
    @commands.guild_only()
    async def boosterrole(self, ctx):
        await self.booster_list(ctx)

    def booster_cfg(self, guild_id):
        store = load_store()
        cfg = store["booster"].setdefault(str(guild_id), {"base": None, "limit": 1, "owned": {}})
        return store, cfg

    @boosterrole.command(name="create")
    @commands.guild_only()
    async def br_create(self, ctx):
        if ctx.author.premium_since is None and not ctx.author.guild_permissions.manage_roles:
            return await ctx.send("Only server boosters can create a booster role.")
        store, cfg = self.booster_cfg(ctx.guild.id)
        owned = cfg["owned"].setdefault(str(ctx.author.id), [])
        if len(owned) >= cfg["limit"]:
            return await ctx.send(f"You've reached the limit of {cfg['limit']} booster role(s).")
        new_role = await ctx.guild.create_role(
            name=ctx.author.display_name, reason=f"Booster role for {ctx.author}"
        )
        base = ctx.guild.get_role(cfg["base"]) if cfg["base"] else None
        if base:
            try:
                await new_role.edit(position=base.position + 1)
            except discord.HTTPException:
                pass
        await ctx.author.add_roles(new_role, reason="Booster role")
        owned.append(new_role.id)
        save_store(store)
        await ctx.send(f"Created your booster role **{new_role.name}**.")

    @boosterrole.command(name="base")
    @commands.has_permissions(manage_roles=True)
    async def br_base(self, ctx, *, query: str = ""):
        store, cfg = self.booster_cfg(ctx.guild.id)
        if not query.strip():
            cfg["base"] = None
            save_store(store)
            return await ctx.send("Cleared the base booster role.")
        role = find_role(ctx.guild, query)
        if not role:
            return await ctx.send("Role not found.")
        cfg["base"] = role.id
        save_store(store)
        await ctx.send(f"Base booster role set to **{role.name}**.")

    @boosterrole.command(name="limit")
    @commands.has_permissions(manage_roles=True)
    async def br_limit(self, ctx, number: int):
        if not 1 <= number <= 25:
            return await ctx.send("Pick a number from 1 to 25.")
        store, cfg = self.booster_cfg(ctx.guild.id)
        cfg["limit"] = number
        save_store(store)
        await ctx.send(f"Each booster can now have up to {number} booster role(s).")

    @boosterrole.command(name="list")
    async def br_list(self, ctx):
        await self.booster_list(ctx)

    async def booster_list(self, ctx):
        _, cfg = self.booster_cfg(ctx.guild.id)
        base = ctx.guild.get_role(cfg["base"]) if cfg["base"] else None
        lines = [
            f"**Base role:** {base.mention if base else 'not set'}",
            f"**Limit per booster:** {cfg['limit']}",
        ]
        for uid, rids in cfg["owned"].items():
            roles = [ctx.guild.get_role(rid) for rid in rids]
            names = ", ".join(r.mention for r in roles if r) or "none"
            lines.append(f"<@{uid}>: {names}")
        await ctx.send("\n".join(lines[:LIST_LIMIT]), allowed_mentions=discord.AllowedMentions.none())

    @boosterrole.command(name="rename")
    @commands.guild_only()
    async def br_rename(self, ctx, *, text: str):
        role, new_name = split_role_and_rest(ctx.guild, text)
        if not role or not new_name:
            return await ctx.send("Usage: `-boosterrole rename <role> <name>`")
        _, cfg = self.booster_cfg(ctx.guild.id)
        owned = cfg["owned"].get(str(ctx.author.id), [])
        if role.id not in owned and not ctx.author.guild_permissions.manage_roles:
            return await ctx.send("That isn't your booster role.")
        await role.edit(name=new_name, reason=f"Booster role renamed by {ctx.author}")
        await ctx.send(f"Renamed the booster role to **{new_name}**.")

    @boosterrole.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def br_remove(self, ctx, *, text: str):
        member, rest = await split_member(ctx, text)
        role = find_role(ctx.guild, rest)
        store, cfg = self.booster_cfg(ctx.guild.id)
        owned = cfg["owned"].get(str(member.id), [])
        if not role or role.id not in owned:
            return await ctx.send("Usage: `-boosterrole remove @user <role>`")
        owned.remove(role.id)
        if not owned:
            cfg["owned"].pop(str(member.id), None)
        save_store(store)
        name = role.name
        await role.delete(reason=f"Booster role removed by {ctx.author}")
        await ctx.send(f"Removed booster role **{name}** from {member.display_name}.")

    @boosterrole.command(name="cleanup")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def br_cleanup(self, ctx):
        store, cfg = self.booster_cfg(ctx.guild.id)
        removed = 0
        for uid in list(cfg["owned"]):
            alive = [rid for rid in cfg["owned"][uid] if ctx.guild.get_role(rid)]
            removed += len(cfg["owned"][uid]) - len(alive)
            if alive:
                cfg["owned"][uid] = alive
            else:
                cfg["owned"].pop(uid)
        save_store(store)
        await ctx.send(f"Removed {removed} deleted role(s) from the booster setup.")

    # Shared helpers

    async def send_role_info(self, ctx, query):
        role = find_role(ctx.guild, query) if query.strip() else ctx.author.top_role
        if not role:
            return await ctx.send("Role not found.")
        embed = discord.Embed(title=role.name, colour=role.colour)
        embed.add_field(name="ID", value=str(role.id))
        embed.add_field(name="Members", value=str(len(role.members)))
        embed.add_field(name="Position", value=str(role.position))
        embed.add_field(name="Mentionable", value=str(role.mentionable))
        embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "D"))
        await ctx.send(embed=embed)

    async def send_in_role(self, ctx, query):
        role = find_role(ctx.guild, query)
        if not role:
            return await ctx.send("Role not found.")
        members = role.members
        names = ", ".join(m.display_name for m in members[:LIST_LIMIT]) or "Nobody"
        extra = f"\n...and {len(members) - LIST_LIMIT} more" if len(members) > LIST_LIMIT else ""
        await ctx.send(
            f"**{role.name}** ({len(members)}): {names}{extra}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def bulk_change(self, ctx, query, add, humans_only=False):
        role = find_role(ctx.guild, query)
        if not role:
            return await ctx.send("Role not found.")
        if err := can_manage(ctx, role):
            return await ctx.send(err)
        targets = [m for m in ctx.guild.members if not (humans_only and m.bot)]
        await self.apply(ctx, role, targets, add=add)

    async def apply(self, ctx, role, members, add):
        done = 0
        for m in members:
            try:
                if add and role not in m.roles:
                    await m.add_roles(role, reason=f"Bulk by {ctx.author}")
                    done += 1
                elif not add and role in m.roles:
                    await m.remove_roles(role, reason=f"Bulk by {ctx.author}")
                    done += 1
            except discord.HTTPException:
                continue
        verb = "added to" if add else "removed from"
        await ctx.send(f"**{role.name}** {verb} {done} member(s).")

    @r.error
    @r_icon.error
    @role.error
    @rrs.error
    @roleinfo.error
    @inrole.error
    @roles.error
    @boosterrole.error
    async def role_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to do that.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("I couldn't find that member.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in a server.")


async def setup(bot):
    await bot.add_cog(RoleCog(bot))