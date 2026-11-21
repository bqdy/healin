"""
Server Configuration (single file)

Roles:      reactionrole, buttonrole
Messages:   autoresponder, automessage, reactiontrigger, webhook, welcome,
            goodbye, boosts, embed, pagination
Community:  starboard, counter, honeypot, bumpreminder

Standalone: needs only discord.py and aiosqlite (pip install aiosqlite).
"""
import asyncio
import json
import re
import time

import aiosqlite
import discord
from discord.ext import commands, tasks


DISBOARD_ID = 302050872383242240


# ======================================================================
# Helpers (storage + embed script parser)
# ======================================================================
class Store:
    """Tiny per-guild JSON key/value store on top of SQLite."""

    def __init__(self, path="server_config.db"):
        self.path = path
        self.db = None

    async def start(self):
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (guild_id, key))"
        )
        await self.db.commit()

    async def get(self, guild_id, key, default=None):
        async with self.db.execute(
            "SELECT value FROM kv WHERE guild_id=? AND key=?", (guild_id, key)
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else default

    async def set(self, guild_id, key, value):
        await self.db.execute(
            "INSERT INTO kv (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, json.dumps(value)),
        )
        await self.db.commit()

    async def delete(self, guild_id, key):
        await self.db.execute("DELETE FROM kv WHERE guild_id=? AND key=?", (guild_id, key))
        await self.db.commit()

    async def prefix(self, guild_id, prefix):
        """All keys in a guild starting with prefix -> {key: value}."""
        async with self.db.execute(
            "SELECT key, value FROM kv WHERE guild_id=? AND key LIKE ?",
            (guild_id, prefix + "%"),
        ) as cur:
            rows = await cur.fetchall()
        return {k: json.loads(v) for k, v in rows}

    async def all(self, key):
        """Same key across every guild -> [(guild_id, value)]."""
        async with self.db.execute("SELECT guild_id, value FROM kv WHERE key=?", (key,)) as cur:
            rows = await cur.fetchall()
        return [(g, json.loads(v)) for g, v in rows]


def fmt(text, member=None, guild=None):
    """Replace variables like {user.mention}, {server}, {count}."""
    guild = guild or (member.guild if member else None)
    rep = {}
    if member:
        rep.update({
            "{user}": str(member),
            "{user.name}": member.display_name,
            "{user.mention}": member.mention,
            "{user.id}": str(member.id),
        })
    if guild:
        rep.update({
            "{server}": guild.name,
            "{count}": str(guild.member_count),
            "{boosts}": str(guild.premium_subscription_count),
        })
    for k, v in rep.items():
        text = text.replace(k, v)
    return text


def build(text, member=None, guild=None):
    """
    Turn a message or embed script into (content, embed).

    Embed script example:
    {embed}{title: Hello}$v{description: Welcome {user.mention}}$v{color: #5865F2}
    Supported: content, title, description, color, footer, image, thumbnail,
    author, field (name|value|inline true/false)
    """
    text = fmt(text, member, guild)
    if not text.startswith("{embed}"):
        return text, None

    content = None
    emb = discord.Embed()
    for seg in text[len("{embed}"):].split("$v"):
        m = re.match(r"\s*\{(\w+):\s*(.*)\}\s*$", seg, re.S)
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip()
        if k == "content":
            content = v
        elif k == "title":
            emb.title = v
        elif k == "description":
            emb.description = v
        elif k == "color":
            try:
                emb.color = discord.Color(int(v.lstrip("#"), 16))
            except ValueError:
                pass
        elif k == "footer":
            emb.set_footer(text=v)
        elif k == "image":
            emb.set_image(url=v)
        elif k == "thumbnail":
            emb.set_thumbnail(url=v)
        elif k == "author":
            emb.set_author(name=v)
        elif k == "field":
            p = [x.strip() for x in v.split("|")]
            if len(p) >= 2:
                emb.add_field(name=p[0], value=p[1], inline=not (len(p) > 2 and p[2].lower() == "false"))
    return content, emb


async def show_subs(ctx):
    """Called when a group is used with no arguments: list its subcommands."""
    cmd = ctx.command
    lines = [f"`{ctx.clean_prefix}{c.qualified_name} {c.signature}`" for c in cmd.commands]
    embed = discord.Embed(
        title=f"{ctx.clean_prefix}{cmd.qualified_name}",
        description="\n".join(lines) or "No subcommands.",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


# ======================================================================
# Views
# ======================================================================
class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"brole:(?P<role>[0-9]+)"):
    """Persistent role button: survives bot restarts."""

    def __init__(self, role_id, label="Role"):
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.primary, custom_id=f"brole:{role_id}"
        ))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["role"]), item.label)

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("That role no longer exists.", ephemeral=True)
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"Gave you **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't manage that role (it's above my highest role).", ephemeral=True
            )


class Paginator(discord.ui.View):
    def __init__(self, pages):
        super().__init__(timeout=None)  # not persistent across restarts
        self.pages = pages
        self.i = 0

    def render(self):
        content, embed = self.pages[self.i]
        label = f"Page {self.i + 1}/{len(self.pages)}"
        if embed is not None:
            if not embed.footer.text or embed.footer.text.startswith("Page "):
                embed.set_footer(text=label)
        else:
            content = f"{content}\n\n*{label}*"
        return content, embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.i = (self.i - 1) % len(self.pages)
        content, embed = self.render()
        await interaction.response.edit_message(content=content, embed=embed, view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.i = (self.i + 1) % len(self.pages)
        content, embed = self.render()
        await interaction.response.edit_message(content=content, embed=embed, view=self)


# ======================================================================
# Cog
# ======================================================================
class ServerConfig(commands.Cog, name="Server Configuration"):
    """Roles, messages and community tools"""

    help_text = """# Server Configuration

## Roles
`{p}reactionrole add <message ID> <emoji> <role>` - Give a role on reaction
`{p}buttonrole send #channel @role <label>` - Send a role button

## Messages
`{p}autoresponder add trigger, response` - Reply to a trigger
`{p}automessage add #channel <minutes> <message>` - Schedule a message
`{p}reactiontrigger add trigger, emoji` - React to a trigger
`{p}webhook send #channel <name>, <message or {embed} code>` - Send a webhook message
`{p}welcome add #channel <message or {embed} code>` - Set a join message
`{p}goodbye add #channel <message or {embed} code>` - Set a leave message
`{p}boosts add #channel <message or {embed} code>` - Set a boost message
`{p}embed send #channel <message or {embed} code>` - Send a supported embed
`{p}pagination send #channel <page 1> || <page 2>` - Send button pagination

## Community
`{p}starboard set #channel [threshold]` - Configure starred-message reposts
`{p}counter set #channel <name>` - Update a channel name with member counts
`{p}honeypot set #channel` - Delete messages sent to a bait channel
`{p}bumpreminder set #channel [hours]` - Schedule bump reminders

Use each command with no arguments to see its subcommands."""

    def __init__(self, bot):
        self.bot = bot
        self.store = Store()
        self.star_lock = asyncio.Lock()
        self.automsg_loop.start()
        self.counter_loop.start()
        self.bump_loop.start()

    async def cog_load(self):
        await self.store.start()
        self.bot.add_dynamic_items(RoleButton)

    async def cog_unload(self):
        if self.store.db:
            await self.store.db.close()
        self.automsg_loop.cancel()
        self.counter_loop.cancel()
        self.bump_loop.cancel()

    # ==================================================================
    # Listeners (all merged here)
    # ==================================================================
    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild:
            return
        gid = message.guild.id

        # honeypot
        hp = await self.store.get(gid, "honeypot")
        if (hp and message.channel.id == hp["channel"] and not message.author.bot
                and not message.author.guild_permissions.administrator):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return

        # bump detection (Disboard)
        if message.author.id == DISBOARD_ID and message.embeds:
            cfg = await self.store.get(gid, "bump")
            if cfg and message.channel.id == cfg["channel"]:
                desc = message.embeds[0].description or ""
                if "bump done" in desc.lower():
                    cfg["next"] = time.time() + cfg["hours"] * 3600
                    await self.store.set(gid, "bump", cfg)
            return

        if message.author.bot:
            return
        text = message.content.lower()

        # autoresponder
        ar = await self.store.get(gid, "autoresponders", {})
        for trigger, response in ar.items():
            if re.search(rf"(?<!\w){re.escape(trigger)}(?!\w)", text):
                content, embed = build(response, message.author, message.guild)
                await message.channel.send(content=content, embed=embed)
                break

        # reaction triggers
        rt = await self.store.get(gid, "reactiontriggers", {})
        for trigger, emoji in rt.items():
            if re.search(rf"(?<!\w){re.escape(trigger)}(?!\w)", text):
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self._reaction_role(payload, True)
        await self._starboard_handle(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self._reaction_role(payload, False)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self._fire("welcome", member)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self._fire("goodbye", member)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.premium_since is None and after.premium_since is not None:
            await self._fire("boosts", after)

    # ==================================================================
    # ROLES
    # ==================================================================
    @commands.group(name="reactionrole", invoke_without_command=True)
    async def reactionrole(self, ctx):
        await show_subs(ctx)

    @reactionrole.command(name="add")
    @commands.has_permissions(manage_roles=True)
    async def rr_add(self, ctx, message_id: int, emoji: str, role: discord.Role):
        """Run in the same channel as the target message."""
        try:
            msg = await ctx.channel.fetch_message(message_id)
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            return await ctx.send("Couldn't find that message here, or the emoji is invalid.")
        key = f"rr:{message_id}"
        data = await self.store.get(ctx.guild.id, key, {})
        data[emoji] = role.id
        await self.store.set(ctx.guild.id, key, data)
        await ctx.send(f"✅ Reacting with {emoji} now gives **{role.name}**.")

    @reactionrole.command(name="remove")
    @commands.has_permissions(manage_roles=True)
    async def rr_remove(self, ctx, message_id: int, emoji: str):
        key = f"rr:{message_id}"
        data = await self.store.get(ctx.guild.id, key, {})
        if data.pop(emoji, None) is None:
            return await ctx.send("No reaction role for that emoji.")
        await self.store.set(ctx.guild.id, key, data)
        await ctx.send("✅ Removed.")

    async def _reaction_role(self, payload, add):
        if not payload.guild_id or payload.user_id == self.bot.user.id:
            return
        data = await self.store.get(payload.guild_id, f"rr:{payload.message_id}")
        if not data:
            return
        role_id = data.get(str(payload.emoji))
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id) if role_id else None
        member = payload.member if add else guild.get_member(payload.user_id)
        if not role or not member:
            return
        try:
            await (member.add_roles(role) if add else member.remove_roles(role))
        except discord.HTTPException:
            pass

    @commands.group(name="buttonrole", invoke_without_command=True)
    async def buttonrole(self, ctx):
        await show_subs(ctx)

    @buttonrole.command(name="send")
    @commands.has_permissions(manage_roles=True)
    async def br_send(self, ctx, channel: discord.TextChannel, role: discord.Role, *, label: str):
        view = discord.ui.View(timeout=None)
        view.add_item(RoleButton(role.id, label))
        await channel.send(f"Click the button to get or remove {role.mention}.",
                           view=view, allowed_mentions=discord.AllowedMentions.none())
        await ctx.message.add_reaction("✅")

    # ==================================================================
    # MESSAGES
    # ==================================================================
    # ---------- autoresponder ----------
    @commands.group(name="autoresponder", invoke_without_command=True)
    async def autoresponder(self, ctx):
        await show_subs(ctx)

    @autoresponder.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def ar_add(self, ctx, *, text: str):
        """Usage: -autoresponder add trigger, response"""
        if "," not in text:
            return await ctx.send("Format: `-autoresponder add trigger, response`")
        trigger, response = (s.strip() for s in text.split(",", 1))
        data = await self.store.get(ctx.guild.id, "autoresponders", {})
        data[trigger.lower()] = response
        await self.store.set(ctx.guild.id, "autoresponders", data)
        await ctx.send(f"✅ I'll reply when someone says `{trigger}`.")

    @autoresponder.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def ar_remove(self, ctx, *, trigger: str):
        data = await self.store.get(ctx.guild.id, "autoresponders", {})
        if data.pop(trigger.lower(), None) is None:
            return await ctx.send("Trigger not found.")
        await self.store.set(ctx.guild.id, "autoresponders", data)
        await ctx.send("✅ Removed.")

    @autoresponder.command(name="list")
    async def ar_list(self, ctx):
        data = await self.store.get(ctx.guild.id, "autoresponders", {})
        await ctx.send(embed=discord.Embed(
            title="Autoresponders",
            description="\n".join(f"`{k}` → {v[:60]}" for k, v in data.items()) or "None set.",
        ))

    # ---------- reaction trigger ----------
    @commands.group(name="reactiontrigger", invoke_without_command=True)
    async def reactiontrigger(self, ctx):
        await show_subs(ctx)

    @reactiontrigger.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def rt_add(self, ctx, *, text: str):
        """Usage: -reactiontrigger add trigger, emoji"""
        if "," not in text:
            return await ctx.send("Format: `-reactiontrigger add trigger, emoji`")
        trigger, emoji = (s.strip() for s in text.split(",", 1))
        try:
            await ctx.message.add_reaction(emoji)  # validates the emoji
        except discord.HTTPException:
            return await ctx.send("I can't use that emoji.")
        data = await self.store.get(ctx.guild.id, "reactiontriggers", {})
        data[trigger.lower()] = emoji
        await self.store.set(ctx.guild.id, "reactiontriggers", data)
        await ctx.send(f"✅ I'll react with {emoji} to `{trigger}`.")

    @reactiontrigger.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def rt_remove(self, ctx, *, trigger: str):
        data = await self.store.get(ctx.guild.id, "reactiontriggers", {})
        if data.pop(trigger.lower(), None) is None:
            return await ctx.send("Trigger not found.")
        await self.store.set(ctx.guild.id, "reactiontriggers", data)
        await ctx.send("✅ Removed.")

    @reactiontrigger.command(name="list")
    async def rt_list(self, ctx):
        data = await self.store.get(ctx.guild.id, "reactiontriggers", {})
        await ctx.send(embed=discord.Embed(
            title="Reaction triggers",
            description="\n".join(f"`{k}` → {v}" for k, v in data.items()) or "None set.",
        ))

    # ---------- automessage ----------
    @commands.group(name="automessage", invoke_without_command=True)
    async def automessage(self, ctx):
        await show_subs(ctx)

    @automessage.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def am_add(self, ctx, channel: discord.TextChannel, minutes: int, *, message: str):
        if minutes < 1:
            return await ctx.send("Minutes must be at least 1.")
        items = await self.store.get(ctx.guild.id, "automessages", [])
        new_id = max((i["id"] for i in items), default=0) + 1
        items.append({"id": new_id, "channel": channel.id, "minutes": minutes,
                      "message": message, "next": time.time() + minutes * 60})
        await self.store.set(ctx.guild.id, "automessages", items)
        await ctx.send(f"✅ Scheduled #{new_id}: every {minutes} min in {channel.mention}.")

    @automessage.command(name="list")
    async def am_list(self, ctx):
        items = await self.store.get(ctx.guild.id, "automessages", [])
        await ctx.send(embed=discord.Embed(
            title="Scheduled messages",
            description="\n".join(
                f"**#{i['id']}** <#{i['channel']}> every {i['minutes']}m — {i['message'][:50]}" for i in items
            ) or "None scheduled.",
        ))

    @automessage.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def am_remove(self, ctx, item_id: int):
        items = await self.store.get(ctx.guild.id, "automessages", [])
        new = [i for i in items if i["id"] != item_id]
        if len(new) == len(items):
            return await ctx.send("No scheduled message with that ID.")
        await self.store.set(ctx.guild.id, "automessages", new)
        await ctx.send("✅ Removed.")

    @tasks.loop(seconds=30)
    async def automsg_loop(self):
        now = time.time()
        for gid, items in await self.store.all("automessages"):
            guild = self.bot.get_guild(gid)
            if not guild:
                continue
            changed = False
            for item in items:
                if now >= item["next"]:
                    item["next"] = now + item["minutes"] * 60
                    changed = True
                    channel = guild.get_channel(item["channel"])
                    if channel:
                        content, embed = build(item["message"], guild=guild)
                        try:
                            await channel.send(content=content, embed=embed)
                        except discord.HTTPException:
                            pass
            if changed:
                await self.store.set(gid, "automessages", items)

    @automsg_loop.before_loop
    async def before_automsg(self):
        await self.bot.wait_until_ready()

    # ---------- webhook ----------
    @commands.group(name="webhook", invoke_without_command=True)
    async def webhook(self, ctx):
        await show_subs(ctx)

    @webhook.command(name="send")
    @commands.has_permissions(manage_webhooks=True)
    async def webhook_send(self, ctx, channel: discord.TextChannel, *, text: str):
        """Usage: -webhook send #channel <name>, <message or {embed} code>"""
        if "," not in text:
            return await ctx.send("Format: `-webhook send #channel name, message`")
        name, body = (s.strip() for s in text.split(",", 1))
        hooks = await channel.webhooks()
        hook = next((h for h in hooks if h.user and h.user.id == self.bot.user.id), None)
        if hook is None:
            hook = await channel.create_webhook(name="Bot Webhook")
        content, embed = build(body, ctx.author, ctx.guild)
        kwargs = {"content": content, "username": name}
        if embed:
            kwargs["embed"] = embed
        await hook.send(**kwargs)
        await ctx.message.add_reaction("✅")

    # ---------- welcome / goodbye / boosts ----------
    async def _fire(self, key, member):
        data = await self.store.get(member.guild.id, key)
        if not data:
            return
        channel = member.guild.get_channel(data["channel"])
        if channel:
            content, embed = build(data["text"], member, member.guild)
            await channel.send(content=content, embed=embed)

    async def _set(self, ctx, key, channel, text):
        await self.store.set(ctx.guild.id, key, {"channel": channel.id, "text": text})
        await ctx.send(f"✅ Saved. Messages will go to {channel.mention}. "
                       "Variables: `{user.mention}` `{user.name}` `{server}` `{count}`")

    async def _clear(self, ctx, key):
        await self.store.delete(ctx.guild.id, key)
        await ctx.send("✅ Removed.")

    @commands.group(name="welcome", invoke_without_command=True)
    async def welcome(self, ctx):
        await show_subs(ctx)

    @welcome.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def welcome_add(self, ctx, channel: discord.TextChannel, *, text: str):
        await self._set(ctx, "welcome", channel, text)

    @welcome.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def welcome_remove(self, ctx):
        await self._clear(ctx, "welcome")

    @welcome.command(name="test")
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx):
        await self._fire("welcome", ctx.author)

    @commands.group(name="goodbye", invoke_without_command=True)
    async def goodbye(self, ctx):
        await show_subs(ctx)

    @goodbye.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def goodbye_add(self, ctx, channel: discord.TextChannel, *, text: str):
        await self._set(ctx, "goodbye", channel, text)

    @goodbye.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def goodbye_remove(self, ctx):
        await self._clear(ctx, "goodbye")

    @goodbye.command(name="test")
    @commands.has_permissions(manage_guild=True)
    async def goodbye_test(self, ctx):
        await self._fire("goodbye", ctx.author)

    @commands.group(name="boosts", invoke_without_command=True)
    async def boosts(self, ctx):
        await show_subs(ctx)

    @boosts.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def boosts_add(self, ctx, channel: discord.TextChannel, *, text: str):
        await self._set(ctx, "boosts", channel, text)

    @boosts.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def boosts_remove(self, ctx):
        await self._clear(ctx, "boosts")

    @boosts.command(name="test")
    @commands.has_permissions(manage_guild=True)
    async def boosts_test(self, ctx):
        await self._fire("boosts", ctx.author)

    # ---------- embed ----------
    @commands.group(name="embed", invoke_without_command=True)
    async def embed_group(self, ctx):
        await show_subs(ctx)
        await ctx.send(
            "Example:\n```\n-embed send #general {embed}{title: Hello}$v"
            "{description: Welcome to {server}!}$v{color: #5865F2}$v{footer: Have fun}\n```"
            "Parts: `content` `title` `description` `color` `footer` `image` `thumbnail` `author` "
            "`field` (name|value|inline)"
        )

    @embed_group.command(name="send")
    @commands.has_permissions(manage_messages=True)
    async def embed_send(self, ctx, channel: discord.TextChannel, *, text: str):
        content, embed = build(text, ctx.author, ctx.guild)
        await channel.send(content=content, embed=embed)
        await ctx.message.add_reaction("✅")

    # ---------- pagination ----------
    @commands.group(name="pagination", invoke_without_command=True)
    async def pagination(self, ctx):
        await show_subs(ctx)

    @pagination.command(name="send")
    @commands.has_permissions(manage_messages=True)
    async def pagination_send(self, ctx, channel: discord.TextChannel, *, text: str):
        """Usage: -pagination send #channel <page 1> || <page 2> || ..."""
        raw = [p.strip() for p in text.split("||") if p.strip()]
        if len(raw) < 2:
            return await ctx.send("Separate at least two pages with `||`.")
        pages = [build(p, ctx.author, ctx.guild) for p in raw]
        view = Paginator(pages)
        content, embed = view.render()
        await channel.send(content=content, embed=embed, view=view)
        await ctx.message.add_reaction("✅")

    # ==================================================================
    # COMMUNITY
    # ==================================================================
    # ---------- starboard ----------
    @commands.group(name="starboard", invoke_without_command=True)
    async def starboard(self, ctx):
        await show_subs(ctx)

    @starboard.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def starboard_set(self, ctx, channel: discord.TextChannel, threshold: int = 3):
        await self.store.set(ctx.guild.id, "starboard", {"channel": channel.id, "threshold": threshold})
        await ctx.send(f"✅ Messages with **{threshold}** ⭐ will be reposted in {channel.mention}.")

    @starboard.command(name="off")
    @commands.has_permissions(manage_guild=True)
    async def starboard_off(self, ctx):
        await self.store.delete(ctx.guild.id, "starboard")
        await ctx.send("✅ Starboard disabled.")

    async def _starboard_handle(self, payload):
        if str(payload.emoji) != "⭐" or not payload.guild_id:
            return
        gid = payload.guild_id
        cfg = await self.store.get(gid, "starboard")
        if not cfg or payload.channel_id == cfg["channel"]:
            return
        guild = self.bot.get_guild(gid)
        channel = guild.get_channel(payload.channel_id)
        star_ch = guild.get_channel(cfg["channel"])
        if not channel or not star_ch:
            return

        async with self.star_lock:
            try:
                msg = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                return
            stars = 0
            for r in msg.reactions:
                if str(r.emoji) == "⭐":
                    async for u in r.users():
                        if u.id != msg.author.id and not u.bot:
                            stars += 1
            if stars < cfg["threshold"]:
                return

            posted = await self.store.get(gid, "starboard:posts", {})
            text = f"⭐ **{stars}** | {channel.mention}"
            if str(msg.id) in posted:
                try:
                    sm = await star_ch.fetch_message(posted[str(msg.id)])
                    await sm.edit(content=text)
                except discord.HTTPException:
                    pass
                return

            embed = discord.Embed(description=msg.content, color=discord.Color.gold(),
                                  timestamp=msg.created_at)
            embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
            embed.add_field(name="Source", value=f"[Jump to message]({msg.jump_url})")
            if msg.attachments:
                embed.set_image(url=msg.attachments[0].url)
            sm = await star_ch.send(text, embed=embed)
            posted[str(msg.id)] = sm.id
            await self.store.set(gid, "starboard:posts", posted)

    # ---------- counter ----------
    @commands.group(name="counter", invoke_without_command=True)
    async def counter(self, ctx):
        await show_subs(ctx)

    @counter.command(name="set")
    @commands.has_permissions(manage_channels=True)
    async def counter_set(self, ctx, channel: discord.abc.GuildChannel, *, name: str):
        """Example: -counter set #voice-channel Members: {count}"""
        if "{count}" not in name:
            name += " {count}"
        await self.store.set(ctx.guild.id, "counter", {"channel": channel.id, "name": name})
        await self.update_counter(ctx.guild)
        await ctx.send("✅ Counter set (updates every 10 minutes because of Discord's rename limits).")

    async def update_counter(self, guild):
        cfg = await self.store.get(guild.id, "counter")
        if not cfg:
            return
        channel = guild.get_channel(cfg["channel"])
        new_name = cfg["name"].replace("{count}", str(guild.member_count))
        if channel and channel.name != new_name:
            try:
                await channel.edit(name=new_name, reason="Member counter")
            except discord.HTTPException:
                pass

    @tasks.loop(minutes=10)
    async def counter_loop(self):
        for guild in self.bot.guilds:
            await self.update_counter(guild)

    @counter_loop.before_loop
    async def before_counter(self):
        await self.bot.wait_until_ready()

    # ---------- honeypot ----------
    @commands.group(name="honeypot", invoke_without_command=True)
    async def honeypot(self, ctx):
        await show_subs(ctx)

    @honeypot.command(name="set")
    @commands.has_permissions(manage_channels=True)
    async def honeypot_set(self, ctx, channel: discord.TextChannel):
        await self.store.set(ctx.guild.id, "honeypot", {"channel": channel.id})
        await ctx.send(f"✅ Anything sent in {channel.mention} will be deleted.")

    @honeypot.command(name="off")
    @commands.has_permissions(manage_channels=True)
    async def honeypot_off(self, ctx):
        await self.store.delete(ctx.guild.id, "honeypot")
        await ctx.send("✅ Honeypot disabled.")

    # ---------- bump reminder ----------
    @commands.group(name="bumpreminder", invoke_without_command=True)
    async def bumpreminder(self, ctx):
        await show_subs(ctx)

    @bumpreminder.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def bump_set(self, ctx, channel: discord.TextChannel, hours: float = 2):
        await self.store.set(ctx.guild.id, "bump", {"channel": channel.id, "hours": hours, "next": None})
        await ctx.send(f"✅ I'll remind you in {channel.mention} {hours}h after each Disboard bump.")

    @bumpreminder.command(name="off")
    @commands.has_permissions(manage_guild=True)
    async def bump_off(self, ctx):
        await self.store.delete(ctx.guild.id, "bump")
        await ctx.send("✅ Bump reminders disabled.")

    @tasks.loop(seconds=30)
    async def bump_loop(self):
        now = time.time()
        for gid, cfg in await self.store.all("bump"):
            if cfg.get("next") and now >= cfg["next"]:
                guild = self.bot.get_guild(gid)
                channel = guild.get_channel(cfg["channel"]) if guild else None
                cfg["next"] = None
                await self.store.set(gid, "bump", cfg)
                if channel:
                    await channel.send("⏰ Time to bump the server! Use `/bump`.")

    @bump_loop.before_loop
    async def before_bump(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    cog = ServerConfig(bot)
    # Skip any command that another cog already registered, so we never clash.
    kept, skipped = [], []
    for cmd in cog.__cog_commands__:
        if any(bot.get_command(n) for n in (cmd.name, *cmd.aliases)):
            skipped.append(cmd.name)
        else:
            kept.append(cmd)
    cog.__cog_commands__ = tuple(kept)
    if skipped:
        print(f"server_config: skipped (already exist): {', '.join(skipped)}")
    await bot.add_cog(cog)
