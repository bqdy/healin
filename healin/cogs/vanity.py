import json
import pathlib

import discord
from discord.ext import commands, tasks

STORE_PATH = pathlib.Path("data/vanity_store.json")
DEFAULT_MESSAGE = "Thanks for repping the server in your status! You've been given {roles}."


def load_store():
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text())
    return {}


def save_store(store):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(store, indent=2))


def guild_cfg(store, guild_id):
    cfg = store.setdefault(str(guild_id), {})
    cfg.setdefault("text", None)
    cfg.setdefault("mode", "substring")
    cfg.setdefault("roles", [])
    cfg.setdefault("message", DEFAULT_MESSAGE)
    return cfg


def custom_status(member):
    for a in member.activities:
        if isinstance(a, discord.CustomActivity):
            return (getattr(a, "state", None) or a.name or "").strip()
    return ""


def matches(cfg, status):
    text = (cfg["text"] or "").strip().lower()
    status = status.strip().lower()
    if not text or not status:
        return False
    if cfg["mode"] == "strict":
        return status == text
    return text in status


def mode_label(cfg):
    if cfg["mode"] == "strict":
        return "strict (the entire custom status must match)"
    return "substring (the custom status must contain the text)"


class Vanity(commands.Cog, name="Vanity"):
    """Award configured roles when a member's custom status matches your text."""

    # general.py shows this page as-is; {p} becomes the bot's prefix
    help_text = (
        "# Vanity\n\n"
        "Award configured roles when a member's custom status matches your text.\n"
        "**Configuration:** Manage Server. The bot needs Manage Roles and a higher role "
        "than each award role; enable Presence and Server Members intents.\n"
        "`{p}vanity` - Show the current matching mode, text, and...\n"
        "`{p}vanity set <text>` - Match custom statuses containing this text\n"
        "`{p}vanity strict <text>` - Require the entire custom status to match\n"
        "`{p}vanity role` - Show role-management guidance\n"
        "`{p}vanity role add @role` - Add a status award role\n"
        "`{p}vanity role remove @role` - Remove a status award role\n"
        "`{p}vanity role list` - List configured award roles\n"
        "`{p}vanity sync` / `{p}vanity check` - Recheck members and update award roles\n"
        "`{p}vanity view` / `{p}vanity view substring` - Show matching text and mode\n"
        "`{p}vanity view message` - Show the award message\n"
        "`{p}vanity message <text>` - Set the award message ({roles} = awarded roles)\n"
        "This status-role system is separate from the server invite vanity lock."
    )

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.reconcile.start()

    async def cog_unload(self):
        self.reconcile.cancel()

    def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # Core logic

    def problems(self, guild, cfg):
        """Things that stop the roles from being awarded, in plain words."""
        out = []
        if not self.bot.intents.presences:
            out.append(
                "The **Presence intent** is off, so I can't read anyone's status. Add "
                "`intents.presences = True` in main.py and switch it on in the Developer Portal "
                "(Bot > Privileged Gateway Intents)."
            )
        if not self.bot.intents.members:
            out.append("The **Server Members intent** is off.")
        me = guild.me
        if not me.guild_permissions.manage_roles:
            out.append("I'm missing the **Manage Roles** permission.")
        for rid in cfg["roles"]:
            role = guild.get_role(rid)
            if role is None:
                out.append(f"An award role was deleted (`{rid}`). Remove it with `vanity role remove`.")
            elif role >= me.top_role:
                out.append(f"**{role.name}** is above my highest role, so I can't give it.")
        return out

    async def update_member(self, member, cfg, notify=True):
        if member.bot or not cfg["text"] or not cfg["roles"]:
            return [], []
        # Offline / invisible members report no activities, which would look like
        # "status removed". Leave their roles alone until we can actually see them.
        if member.status is discord.Status.offline:
            return [], []
        wanted = matches(cfg, custom_status(member))
        me = member.guild.me
        added, removed = [], []
        for rid in cfg["roles"]:
            role = member.guild.get_role(rid)
            if not role or role >= me.top_role:
                continue
            try:
                if wanted and role not in member.roles:
                    await member.add_roles(role, reason="Vanity status match")
                    added.append(role)
                elif not wanted and role in member.roles:
                    await member.remove_roles(role, reason="Vanity status removed")
                    removed.append(role)
            except discord.HTTPException:
                continue
        if added and notify:
            text = cfg["message"].replace("{user}", member.display_name)
            text = text.replace("{roles}", ", ".join(r.name for r in added))
            try:
                await member.send(text)
            except discord.HTTPException:
                pass
        return added, removed

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        if after.bot or custom_status(before) == custom_status(after):
            return
        cfg = guild_cfg(load_store(), after.guild.id)
        await self.update_member(after, cfg)

    # Safety net: recheck online members every few minutes and at startup, so
    # people who already had the status (or whose update was missed) still get the role.

    @tasks.loop(minutes=5)
    async def reconcile(self):
        store = load_store()
        for guild in self.bot.guilds:
            if str(guild.id) not in store:
                continue
            cfg = guild_cfg(store, guild.id)
            if not cfg["text"] or not cfg["roles"]:
                continue
            try:
                for member in guild.members:
                    await self.update_member(member, cfg)
            except Exception as e:
                print(f"[vanity] recheck failed in {guild.name}: {e!r}")

    @reconcile.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()
        if not self.bot.intents.presences:
            print(
                "[vanity] Presence intent is OFF: custom statuses can't be read. "
                "Set intents.presences = True in main.py and enable it in the Developer Portal."
            )

    # -vanity

    @commands.group(name="vanity", invoke_without_command=True, help="Show the current matching mode, text, and award roles")
    async def vanity(self, ctx):
        cfg = guild_cfg(load_store(), ctx.guild.id)
        roles = [ctx.guild.get_role(r) for r in cfg["roles"]]
        role_text = ", ".join(r.mention for r in roles if r) or "none"
        text = (
            f"**Mode:** {mode_label(cfg)}\n"
            f"**Text:** {cfg['text'] or 'not set'}\n"
            f"**Award roles:** {role_text}"
        )
        issues = self.problems(ctx.guild, cfg)
        if issues:
            text += "\n\n\u26a0\ufe0f **Needs attention:**\n" + "\n".join(f"- {i}" for i in issues)
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    @vanity.command(name="set", help="Match custom statuses containing this text")
    @commands.has_permissions(manage_guild=True)
    async def vanity_set(self, ctx, *, text: str):
        store = load_store()
        cfg = guild_cfg(store, ctx.guild.id)
        cfg["text"], cfg["mode"] = text.strip(), "substring"
        save_store(store)
        await ctx.send(f"Members whose custom status contains `{cfg['text']}` will get the award roles.")

    @vanity.command(name="strict", help="Require the entire custom status to match")
    @commands.has_permissions(manage_guild=True)
    async def vanity_strict(self, ctx, *, text: str):
        store = load_store()
        cfg = guild_cfg(store, ctx.guild.id)
        cfg["text"], cfg["mode"] = text.strip(), "strict"
        save_store(store)
        await ctx.send(f"Members whose whole custom status is `{cfg['text']}` will get the award roles.")

    @vanity.command(name="message", help="Set the award message ({roles} = awarded roles)")
    @commands.has_permissions(manage_guild=True)
    async def vanity_message(self, ctx, *, text: str):
        store = load_store()
        guild_cfg(store, ctx.guild.id)["message"] = text.strip()
        save_store(store)
        await ctx.send("Award message updated.")

    # -vanity role

    @vanity.group(name="role", invoke_without_command=True, help="Show role-management guidance")
    async def vanity_role(self, ctx):
        p = ctx.clean_prefix
        await ctx.send(
            f"Manage status award roles with `{p}vanity role add @role`, "
            f"`{p}vanity role remove @role` and `{p}vanity role list`.\n"
            "The bot needs **Manage Roles**, and its highest role must sit above every award role."
        )

    @vanity_role.command(name="add", help="Add a status award role")
    @commands.has_permissions(manage_guild=True)
    async def vanity_role_add(self, ctx, *, role: discord.Role):
        if role.is_default() or role.managed:
            return await ctx.send("That role can't be used as an award role.")
        if role >= ctx.guild.me.top_role:
            return await ctx.send(f"I can't give **{role.name}** because it's above my highest role.")
        store = load_store()
        cfg = guild_cfg(store, ctx.guild.id)
        if role.id in cfg["roles"]:
            return await ctx.send("That role is already an award role.")
        cfg["roles"].append(role.id)
        save_store(store)
        await ctx.send(f"Added **{role.name}** as a status award role.")

    @vanity_role.command(name="remove", help="Remove a status award role")
    @commands.has_permissions(manage_guild=True)
    async def vanity_role_remove(self, ctx, *, role: discord.Role):
        store = load_store()
        cfg = guild_cfg(store, ctx.guild.id)
        if role.id not in cfg["roles"]:
            return await ctx.send("That role isn't an award role.")
        cfg["roles"].remove(role.id)
        save_store(store)
        await ctx.send(f"Removed **{role.name}** from the status award roles.")

    @vanity_role.command(name="list", help="List configured award roles")
    async def vanity_role_list(self, ctx):
        cfg = guild_cfg(load_store(), ctx.guild.id)
        roles = [ctx.guild.get_role(r) for r in cfg["roles"]]
        roles = [r for r in roles if r]
        if not roles:
            return await ctx.send("No award roles configured.")
        await ctx.send(
            "**Award roles:** " + ", ".join(r.mention for r in roles),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -vanity sync / check

    @vanity.command(name="sync", aliases=["check"], help="Recheck members and update award roles")
    @commands.has_permissions(manage_guild=True)
    async def vanity_sync(self, ctx):
        cfg = guild_cfg(load_store(), ctx.guild.id)
        if not cfg["text"] or not cfg["roles"]:
            return await ctx.send("Set the text and at least one award role first.")
        if not self.bot.intents.presences:
            return await ctx.send(
                "I can't read statuses because the **Presence intent** is off. Add "
                "`intents.presences = True` in main.py, enable it in the Developer Portal, "
                "and restart me."
            )
        gave = took = 0
        async with ctx.typing():
            for member in ctx.guild.members:
                added, removed = await self.update_member(member, cfg, notify=False)
                gave += bool(added)
                took += bool(removed)
        await ctx.send(f"Sync done. Awarded roles to {gave} member(s) and removed them from {took}.")

    # -vanity view

    @vanity.group(name="view", invoke_without_command=True, help="Show matching text and mode")
    async def vanity_view(self, ctx):
        await self.show_text(ctx)

    @vanity_view.command(name="substring", help="Show matching text and mode")
    async def vanity_view_substring(self, ctx):
        await self.show_text(ctx)

    @vanity_view.command(name="message", help="Show the award message")
    async def vanity_view_message(self, ctx):
        cfg = guild_cfg(load_store(), ctx.guild.id)
        await ctx.send(f"**Award message:** {cfg['message']}")

    async def show_text(self, ctx):
        cfg = guild_cfg(load_store(), ctx.guild.id)
        await ctx.send(f"**Text:** {cfg['text'] or 'not set'}\n**Mode:** {mode_label(cfg)}")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Manage Server to do that.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in a server.")
        elif isinstance(error, (commands.RoleNotFound, commands.BadArgument)):
            await ctx.send("I couldn't find that role.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Vanity(bot))
