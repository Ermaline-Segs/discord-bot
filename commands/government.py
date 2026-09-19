
"""Government cog: !appoint, !dismiss, !government.

This file owns everything about DELTA CITY government offices:
- !appoint  : give a Discord role for an office (admin only)
- !dismiss  : remove someone from an office (admin only)
- !government : show who currently holds every office

How it fits together:
1. An admin types a command like `!appoint @user Governor Asaba`.
2. helpers.parse_role_input() turns that text into a real office
   (title, state, Discord role name, unique-or-multi holder).
3. The citizen database is checked (a member must be a registered
   citizen before holding office, if the setting is on).
4. The Discord role is found or created, with a role-hierarchy check
   so the bot never fails with a raw Discord error.
5. For unique offices the previous holder loses the role and the
   replacement is recorded in the audit log.
6. The database records the appointment so government state survives
   bot restarts.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from config import settings
from delta_city import permissions
from utils import helpers


class Government(commands.Cog):
    """DELTA CITY government: appointments, dismissals, directory."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        """Short path to the shared database (attached to the bot)."""
        return self.bot.db

    # ------------------------------------------------------------------
    # !appoint
    # ------------------------------------------------------------------
    @commands.command(
        name="appoint",
        help="Appoint someone to a government office. "
             "Examples: `!appoint @user President` or `!appoint @user Governor Asaba` "
             "(admins, President, Vice President, Chief of Staff, or the Chief Administrator).",
    )
    async def appoint(self, ctx, member: discord.Member, *, role_input: str):
        ok, reason = permissions.can_appoint(ctx.author)
        if not ok:
            await ctx.reply(reason, mention_author=False)
            return
        office = helpers.parse_role_input(role_input)
        if office is None:
            await ctx.send(
                "⚠️ I don't recognize that office. "
                "Examples: `!appoint @user President`, `!appoint @user Governor Asaba`."
            )
            return
        if office.get("missing_state"):
            await ctx.send(
                f"⚠️ **{office['title']}** is a state office, so a state name is "
                f"required. Try: `!appoint {member.mention} {office['title']} Asaba`"
            )
            return

        # ---- eligibility: must be a registered citizen ------------
        citizen = self.db.get_citizen(member.id)
        if settings.REQUIRE_CITIZEN_FOR_APPOINTMENT and citizen is None:
            await ctx.send(
                f"⚠️ {helpers.mention(member)} is not registered in the Delta City "
                "citizen registry. They must finish `!register` before they can "
                "hold a government office."
            )
            return

        # ---- Discord role setup ------------------------------------
        role, err = await helpers.get_or_create_role(ctx.guild, office["role_name"])
        if role is None:
            await ctx.send(f"⚠️ {err}")
            return

        hierarchy_err = helpers.hierarchy_error(ctx.guild, role)
        if hierarchy_err:
            await ctx.send(f"⚠️ {hierarchy_err}")
            return

        if member.id == ctx.guild.me.id:
            await ctx.send("⚠️ I can't appoint myself to government office.")
            return

        # ---- unique offices: replace the previous holder -----------
        replaced_id = None
        if office["kind"] == "unique":
            previous = self.db.appointment_holder(office["role_name"], office["state"])
            if previous and int(previous["discord_id"]) != member.id:
                replaced_id = int(previous["discord_id"])
            for old in role.members:
                if old.id == member.id:
                    continue
                try:
                    await old.remove_roles(
                        role,
                        reason=f"Replaced as {office['role_name']} by {member.display_name}",
                    )
                except discord.Forbidden:
                    pass  # will be reported after the add attempt below

        # ---- give the role -----------------------------------------
        try:
            await member.add_roles(
                role, reason=f"Appointed {office['role_name']} by {ctx.author.display_name}"
            )
        except discord.Forbidden:
            fallback = (
                "I don't have permission to assign that role. "
                "Make sure my highest role is above it in Server Settings -> Roles."
            )
            message = (f"⚠️ {hierarchy_err}") if hierarchy_err else ("⚠️ " + fallback)
            await ctx.send(message)
            return

        # ---- record it ---------------------------------------------
        self.db.log_appointment(
            member.id, office["role_name"], office["state"], actor_id=ctx.author.id
        )
        if replaced_id is not None:
            self.db.void_appointment(
                replaced_id,
                office["role_name"],
                office["state"],
                actor_id=ctx.author.id,
            )

        citizen_tag = f" ({citizen['citizen_id']})" if citizen else ""
        await ctx.send(
            f"🏛️ {helpers.mention(member)}{citizen_tag} has been appointed "
            f"**{office['role_name']}**."
        )

    # ------------------------------------------------------------------
    # !dismiss
    # ------------------------------------------------------------------
    @commands.command(
        name="dismiss",
        help="Remove someone from a government office. "
             "Examples: `!dismiss @user Governor Asaba` or `!dismiss @user Senator` "
             "(admins, President, Vice President, Chief of Staff, or the Chief Administrator).",
    )
    async def dismiss(self, ctx, member: discord.Member, *, role_input: str):
        ok, reason = permissions.can_appoint(ctx.author)
        if not ok:
            await ctx.reply(reason, mention_author=False)
            return
        office = helpers.parse_role_input(role_input)
        if office is None:
            await ctx.send(
                "⚠️ I don't recognize that office. "
                "Examples: `!dismiss @user Senator`, `!dismiss @user Governor Asaba`."
            )
            return
        if office.get("missing_state"):
            await ctx.send(
                f"⚠️ **{office['title']}** is a state office, so a state name is "
                f"required. Try: `!dismiss {member.mention} {office['title']} Asaba`"
            )
            return

        role = helpers.find_role(ctx.guild, office["role_name"])
        if role is None or role not in member.roles:
            await ctx.send(
                f"⚠️ {helpers.mention(member)} doesn't currently hold **{office['role_name']}**."
            )
            return

        try:
            await member.remove_roles(
                role, reason=f"Dismissed from {office['role_name']} by {ctx.author.display_name}"
            )
        except discord.Forbidden:
            hierarchy_err = helpers.hierarchy_error(ctx.guild, role)
            await ctx.send(
                (f"⚠️ {hierarchy_err}" if hierarchy_err
                    else "⚠️ I don't have permission to remove that role.")
            )
            return

        had_record = self.db.void_appointment(
            member.id, office["role_name"], office["state"], actor_id=ctx.author.id
        )
        if not had_record:
            # Role existed in Discord but was never recorded (e.g. assigned
            # by hand before the bot was upgraded) - still audit it.
            self.db.log_audit(
                ctx.author.id, "dismiss_unrecorded", member.id,
                f"Removed {office['role_name']} role (no database record found)",
            )

        citizen = self.db.get_citizen(member.id)
        citizen_tag = f" ({citizen['citizen_id']})" if citizen else ""
        await ctx.send(
            f"🏛️ {helpers.mention(member)}{citizen_tag} has been dismissed from "
            f"**{office['role_name']}**."
        )

    # ------------------------------------------------------------------
    # !government
    # ------------------------------------------------------------------
    @commands.command(
        name="government",
        help="Show who currently holds every DELTA CITY office.",
    )
    async def government(self, ctx):
        lines = ["🏛️ **DELTA CITY — NATIONAL GOVERNMENT**"]

        for title in settings.NATIONAL_ROLES:
            lines.append(f"• **{title}:** {self._holder_text(ctx, title)}")

        lines.append("")
        lines.append("**National Legislature:**")
        for title in settings.NATIONAL_MULTI_ROLES:
            lines.append(f"• **{title}:** {self._holder_text(ctx, title)}")

        for state in settings.STATES:
            lines.append("")
            lines.append(f"**{state.upper()} STATE GOVERNMENT**")
            for title in settings.STATE_ROLES:
                lines.append(
                    f"• **{title}:** {self._holder_text(ctx, f'{title} of {state}')}"
                )
            lines.append("")
            lines.append(f"**{state.upper()} STATE LEGISLATURE:**")
            for title in settings.STATE_MULTI_ROLES:
                lines.append(
                    f"• **{title}:** {self._holder_text(ctx, f'{title} of {state}')}"
                )

        await ctx.send("\n".join(lines))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _holder_text(self, ctx, role_name: str) -> str:
        """'Alice, Bob' or '*vacant*' for the members of a role."""
        role = helpers.find_role(ctx.guild, role_name)
        if role is None or not role.members:
            return "*vacant*"
        return ", ".join(m.display_name for m in role.members)

async def setup(bot: commands.Bot):
    await bot.add_cog(Government(bot))
