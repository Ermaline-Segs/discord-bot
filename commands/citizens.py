"""Citizen commands for DELTA CITY.

This file owns every command that reads or edits the citizen registry:

- !citizen              your own profile (admins: !citizen @user)
- !citizen search <q>   registry search (admin only)
- !citizen edit @user   edit name/gender/state/status (admin only)
- !citizens              paginated full registry (admin only)
- !move @user <state>    move a citizen between states (admin only)
- !stateinfo <state>     state government + population summary
- !dchelp                command help, organised by category

Every command is a thin shell: the actual reads/writes happen through
DeltaCityDB (database/database.py) and the display logic uses the shared
formatters in utils/helpers.py.  Admin-only commands use the
`admin_only` guard below so ordinary citizens can never edit or search
the registry.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from config import settings
from utils import helpers

# How many registry entries to show per page in !citizens.
REGISTRY_PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# Shared formatting
# ---------------------------------------------------------------------------

def format_profile(row: dict) -> str:
    """Render one citizen record as a readable Discord message."""
    lines = [
        "**🪪 DELTA CITY CITIZEN PROFILE**",
        f"ID: `{row['citizen_id']}`",
        f"Name: {row['name']}",
        f"Gender: {row['gender']}",
        f"State: {row['state']}",
        f"Status: {row['status']}",
        f"Registered: {helpers.format_date(row['joined_at'])}",
    ]
    if row["registered_state"] != row["state"]:
        lines.append(
            f"Original state: {row['registered_state']} "
            f"(registered there, now resides in {row['state']})"
        )
    return "\n".join(lines)


def _resolve_state(name: str):
    """Case-insensitive state lookup. Returns the canonical name or None."""
    for state in settings.STATES:
        if state.lower() == name.strip().lower():
            return state
    return None


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def admin_only():
    """Decorator: refuse non-admins before the command body runs."""
    def decorator(func):
        @commands.wraps(func)
        async def wrapper(ctx: commands.Context, *args, **kwargs):
            if not ctx.guild or not ctx.author.guild_permissions.administrator:
                await ctx.send(
                    "⛔ That command is restricted to Delta City "
                    "administrators."
                )
                return
            return await func(ctx, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# !citizens pagination view
# ---------------------------------------------------------------------------

class RegistryView(discord.ui.View):
    """Previous / Next buttons that flip through registry pages."""

    def __init__(self, rows: list):
        super().__init__(timeout=180)
        self.rows = rows
        self.page = 0

    @property
    def page_count(self) -> int:
        return max(1, (len(self.rows) + REGISTRY_PAGE_SIZE - 1) // REGISTRY_PAGE_SIZE)

    def render(self) -> str:
        start = self.page * REGISTRY_PAGE_SIZE
        chunk = self.rows[start:start + REGISTRY_PAGE_SIZE]
        if not chunk:
            body = "*The registry is empty. No one has registered yet.*"
        else:
            body = "\n".join(
                f"`{r['citizen_id']}` » {r['name']} » {r['state']} » {r['status']}"
                for r in chunk
            )
        header = (
            f"**🏛️ DELTA CITY — CITIZEN REGISTRY** "
            f"(page {self.page + 1}/{self.page_count}, {len(self.rows)} total)"
        )
        return f"{header}\n{body}"

    @discord.ui.button(label="Previous", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(content=self.render())
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button):
        if self.page < self.page_count - 1:
            self.page += 1
            await interaction.response.edit_message(content=self.render())
        else:
            await interaction.response.defer()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------

class Citizens(commands.Cog):
    """DELTA CITY citizen registry commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ---- !citizen group ----------------------------------------------------

    @commands.group(invoke_without_command=True)
    async def citizen(self, ctx: commands.Context, member: discord.Member = None):
        """Show a citizen profile. Admins may look up other members."""
        if member is not None and member.id != ctx.author.id:
            if not ctx.author.guild_permissions.administrator:
                await ctx.send(
                    "⛔ Only administrators may view other people's "
                    "profiles. Use `!citizen` for your own."
                )
                return
            target = member
        else:
            target = ctx.author

        row = self.db.get_citizen(target.id)
        if row is None:
            await ctx.send(
                "🪪 No citizen record found. "
                f"{'They must' if target.id != ctx.author.id else 'You must'} "
                "complete registration with `!register` first."
            )
            return
        await ctx.send(format_profile(row))

    @citizen.command(name="search")
    @admin_only()
    async def citizen_search(self, ctx: commands.Context, *, query: str):
        """Search the registry by name or citizen ID (admin only)."""
        results = self.db.search_citizens(query)
        if not results:
            await ctx.send(f"🔎 No citizens match `{query}`.")
            return
        if len(results) > 25:
            results = results[:25]
        body = "\n".join(
            f"`{r['citizen_id']}` » {r['name']} » {r['state']} » {r['status']}"
            for r in results
        )
        await ctx.send(f"🔎 **Search results for `{query}`**\n{body}")

    @citizen.command(name="edit")
    @admin_only()
    async def citizen_edit(
        self,
        ctx: commands.Context,
        member: discord.Member,
        field: str,
        *,
        value: str,
    ):
        """Edit a citizen record: name, gender, state or status (admin only)."""
        row = self.db.get_citizen(member.id)
        if row is None:
            await ctx.send(
                f"⚠️ {helpers.mention(member)} has no citizen record, so there "
                "is nothing to edit. Ask them to run `!register` first."
            )
            return

        field = field.strip().lower()
        if field == "name":
            value = value.strip()
            if not value:
                await ctx.send("⚠️ A citizen name cannot be empty.")
                return
            if len(value) > 64:
                await ctx.send("⚠️ Citizen names are limited to 64 characters.")
                return
            changes = self.db.update_citizen(
                member.id, actor_id=ctx.author.id, name=value
            )
        elif field == "gender":
            match = next(
                (g for g in settings.GENDER_OPTIONS
                 if g.lower() == value.strip().lower()),
                None,
            )
            if match is None:
                await ctx.send(
                    "⚠️ Gender must be one of: "
                    + ", ".join(settings.GENDER_OPTIONS)
                    + "."
                )
                return
            changes = self.db.update_citizen(
                member.id, actor_id=ctx.author.id, gender=match
            )
        elif field == "state":
            state = _resolve_state(value)
            if state is None:
                await ctx.send(
                    "⚠️ Unknown state. Valid states: "
                    + ", ".join(settings.STATES)
                    + "."
                )
                return
            if state != row["state"]:
                # !citizen edit ... state also swaps the Discord state role.
                changes = self.db.change_state(
                    member.id, state, actor_id=ctx.author.id
                )
                await _swap_state_roles(ctx, member, row["state"], state)
                if changes is False or changes is None:
                    await ctx.send(
                        f"ℹ️ {helpers.mention(member)} already lives in "
                        f"{state}."
                    )
                    return
            else:
                await ctx.send(
                    f"ℹ️ {helpers.mention(member)} already lives in {state}."
                )
                return
        elif field == "status":
            allowed = list(settings.CITIZEN_STATUSES) + [settings.INACTIVE_STATUS]
            match = next(
                (s for s in allowed if s.lower() == value.strip().lower()), None
            )
            if match is None:
                await ctx.send(
                    "⚠️ Status must be one of: " + ", ".join(allowed) + "."
                )
                return
            changes = self.db.set_status(
                member.id, match, actor_id=ctx.author.id
            )
        else:
            await ctx.send(
                "⚠️ I don't know that field. Editable fields: "
                "`name`, `gender`, `state`, `status`.\n"
                "Example: `!citizen edit @user name John Doe`"
            )
            return

        if field in ("name", "gender", "status") and not changes:
            await ctx.send("ℹ️ That field already had the same value.")
            return

        updated = self.db.get_citizen(member.id)
        await ctx.send(
            f"✅ Updated {helpers.mention(member)}:\n{format_profile(updated)}"
        )

    # ---- !citizens ----------------------------------------------------------

    @commands.command(name="citizens")
    @admin_only()
    async def citizens(self, ctx: commands.Context):
        """Paginated view of the full citizen registry (admin only)."""
        rows = self.db.all_citizens()
        view = RegistryView(rows)
        await ctx.send(content=view.render(), view=view)

    # ---- !move ---------------------------------------------------------------

    @commands.command(name="move")
    @admin_only()
    async def move(
        self, ctx: commands.Context, member: discord.Member, state: str
    ):
        """Move a citizen to a new state of residence (admin only)."""
        row = self.db.get_citizen(member.id)
        if row is None:
            await ctx.send(
                f"⚠️ {helpers.mention(member)} is not registered in the "
                "Delta City registry. Ask them to run `!register` first."
            )
            return

        target_state = _resolve_state(state)
        if target_state is None:
            await ctx.send(
                "⚠️ Unknown state. Valid states: "
                + ", ".join(settings.STATES)
                + "."
            )
            return

        if target_state == row["state"]:
            await ctx.send(
                f"ℹ️ {helpers.mention(member)} already resides in {target_state}."
            )
            return

        # 1. Change the residence in the database (ID never changes).
        self.db.change_state(member.id, target_state, actor_id=ctx.author.id)
        # 2. Swap the Discord state role.
        await _swap_state_roles(ctx, member, row["state"], target_state)
        # 3. Confirm with the fresh record.
        updated = self.db.get_citizen(member.id)
        await ctx.send(
            f"✅ {helpers.mention(member)} moved from **{row['state']}** to "
            f"**{target_state}**.\n"
            f"Citizen ID unchanged: `{updated['citizen_id']}`\n"
            f"{format_profile(updated)}"
        )

    # ---- !stateinfo ------------------------------------------------------------

    @commands.command(name="stateinfo")
    async def stateinfo(self, ctx: commands.Context, state: str):
        """Show a state's government and population summary."""
        resolved = _resolve_state(state)
        if resolved is None:
            await ctx.send(
                "⚠️ Unknown state. Valid states: "
                + ", ".join(settings.STATES)
                + "."
            )
            return

        counts = self.db.count_by_state_and_status().get(resolved, {})
        citizens_n = counts.get("Citizen", 0)
        residents_n = counts.get("Resident", 0)
        visitors_n = counts.get("Visitor", 0)
        total = self.db.citizen_count(resolved)

        lines = [f"**🏙️ {resolved.upper()}**"]
        for title in settings.STATE_ROLES:
            role_name = helpers.state_role_name(title, resolved)
            holder = self.db.appointment_holder(title, resolved)
            if holder:
                row = self.db.get_citizen(holder["discord_id"])
                display = row["name"] if row else holder["discord_id"]
                lines.append(f"{title}: {display}")
            else:
                lines.append(f"{title}: *vacant*")
        for title in settings.STATE_MULTI_ROLES:
            role_name = helpers.state_role_name(title, resolved)
            appointments = [
                a for a in self.db.active_appointments()
                if a["role_name"] == role_name
            ]
            if appointments:
                names = []
                for a in appointments:
                    row = self.db.get_citizen(a["discord_id"])
                    names.append(row["name"] if row else a["discord_id"])
                lines.append(f"{title}s: " + ", ".join(names))
            else:
                lines.append(f"{title}s: *none appointed*")

        lines.append("")
        lines.append(
            f"👥 Population: **{total}** "
            f"(Citizens: {citizens_n}, Residents: {residents_n}, "
            f"Visitors: {visitors_n})"
        )
        await ctx.send("\n".join(lines))

    # ---- !dchelp --------------------------------------------------------------

    @commands.command(name="dchelp")
    async def dchelp(self, ctx: commands.Context):
        """Show the Delta City command list, grouped by category."""
        is_admin = (
            ctx.guild is not None
            and ctx.author.guild_permissions.administrator
        )
        lines = [
            "🏛️ **DELTA CITY — COMMANDS**",
            "",
            "**🏛️ Government** (admin)",
            "`!appoint @user <role> [state]` — appoint someone to office",
            "`!dismiss @user <role> [state]` — remove someone from office",
            "`!government` — full directory of national + state offices",
            "",
            "**📋 Registration** (everyone)",
            "`!register` — register as a citizen, resident, or visitor",
            "`!dchelp` — this command list",
            "",
            "**👤 Citizen registry** (admin)",
            "`!citizen` — your own profile",
            "`!citizen @user` — someone else's profile",
            "`!citizen search <query>` — search the registry",
            "`!citizen edit @user <field> <value>` — edit name/gender/state/status",
            "`!citizens` — full paginated registry",
            "`!move @user <state>` — move a citizen between states",
            "`!stateinfo <state>` — state government + population summary",
        ]
        if not is_admin:
            lines.append("")
            lines.append("_Some commands are hidden — you need the Administrator permission._")
        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    """Called by discord.py when the cog is loaded from bot.py."""
    await bot.add_cog(CitizensCog(bot))