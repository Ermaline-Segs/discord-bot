"""DELTA CITY registration flow: the `!register` command.

What this file does
-------------------
A brand-new member types `!register` and the bot walks them through a
guided Discord UI flow instead of making them type long commands:

    Start button -> name modal -> gender select -> state select
    -> status select -> confirm screen

Only when the user presses **Confirm** does a permanent citizen record
get created in the database (database/database.py).  The citizen ID is
state-based (DC-ASB-0001, DC-WAR-0001, ...) and the matching Discord
state role is assigned.

Guard rails implemented here:
- a member can only ever have ONE active citizen profile (duplicates
  are rejected and their current profile is shown instead);
- a session that is abandoned for REGISTRATION_TIMEOUT_SECONDS expires:
  no record is created, no number is reserved, and the user can start
  again;
- every choice is validated against the lists in config/settings.py,
  so free-text states/genders/statuses can never reach the database.
"""

import time

import discord
from discord.ext import commands

from config import settings
from utils import helpers

# How long a registration session may stay open (seconds).
REGISTRATION_TIMEOUT_SECONDS = 600  # 10 minutes


class RegistrationCancelled(Exception):
    """Raised internally when the user presses Cancel."""


class NameModal(discord.ui.Modal):
    """First step: a small Discord modal asking for the citizen name."""

    def __init__(self, cog: "RegistrationCog"):
        super().__init__(title="DELTA CITY REGISTRATION")
        self._cog = cog
        self.name_input = discord.ui.TextInput(
            label="What is your citizen name?",
            placeholder="e.g. John Doe",
            min_length=2,
            max_length=50,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.name_input.value.strip()
        if len(raw) < 2:
            await interaction.response.send_message(
                "That name is too short. Please use at least 2 characters.",
                ephemeral=True,
            )
            return
        session = self._cog.sessions.get((interaction.guild_id, interaction.user.id))
        if session is None or not self._cog._session_alive(session):
            # The modal was submitted after the session already expired.
            await interaction.response.send_message(
                "That registration session has expired. Run `!register` to start over.",
                ephemeral=True,
            )
            return
        session["name"] = raw
        # The modal response becomes the next step (gender select).
        await interaction.response.send_message(
            "**What is your gender?** Select one below.",
            view=self._cog.gender_view(session),
            ephemeral=True,
        )


class RegistrationCog(commands.Cog):
    """The `!register` command and the step-by-step UI behind it."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # One in-flight registration per (guild, user):
        # (guild_id, user_id) -> session dict
        self.sessions = {}
        # The message id of the modal currently on screen (used by
        # NameModal to verify the session is still live).
        self._pending_modal_message_id = None

    # ------------------------------------------------------------------
    # lifecycle helpers
    # ------------------------------------------------------------------

    def _new_session(self, guild_id: int, user_id: int, message: discord.Message) -> dict:
        session = {
            "message": message,
            "started_at": time.time(),
            "name": None,
            "gender": None,
            "state": None,
            "status": None,
        }
        self.sessions[(guild_id, user_id)] = session
        return session

    def _kill_session(self, guild_id: int, user_id: int):
        self.sessions.pop((guild_id, user_id), None)

    def _session_alive(self, session: dict) -> bool:
        return (time.time() - session["started_at"]) <= REGISTRATION_TIMEOUT_SECONDS

    def _log_audit(self, action: str, subject_id=None, details: str = None):
        """Write an audit-log entry for a registration event."""
        self.db.log_audit(
            actor_id=None, action=action, subject_id=subject_id, details=details
        )

    async def _expire_session(self, session: dict):
        """Turn a live registration message into an 'expired' notice."""
        message = session["message"]
        try:
            await message.edit(
                content=(
                    "⌛ **Registration expired.** No record was created and no "
                    "citizen number was reserved. You can start again any time "
                    "with `!register`."
                ),
                view=None,
            )
        except discord.HTTPException:
            pass  # message was deleted; nothing left to update

    async def _sweep_expired(self):
        """Drop expired sessions from memory and mark their message."""
        for (guild_id, user_id), session in list(self.sessions.items()):
            if not self._session_alive(session):
                self._kill_session(guild_id, user_id)
                await self._expire_session(session)

    # ------------------------------------------------------------------
    # the !register command
    # ------------------------------------------------------------------

    @commands.command(
        name="register",
        help="Register yourself as a Delta City citizen/resident/visitor.",
    )
    async def register(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Registration is only available inside the Delta City server.")
            return

        existing = self.db.get_citizen(ctx.author.id)
        if existing is not None:
            if existing["status"] == settings.INACTIVE_STATUS:
                # They were registered before and left; record their return
                # and reactivate them. They keep their ORIGINAL citizen ID.
                self.db.set_status(ctx.author.id, "Citizen", actor_id=ctx.author.id)
                self.db.log_audit(
                    actor_id=ctx.author.id,
                    action="return",
                    subject_id=ctx.author.id,
                    details="Rejoined after leaving; profile reactivated",
                )
                await ctx.send(
                    f"Welcome back, **{existing['name']}**! You were reactivated "
                    f"with your original ID `{existing['citizen_id']}`. "
                    "You do not get a new number."
                )
                return
            profile = self._format_profile(existing)
            await ctx.send(
                "You are already registered in Delta City — one Discord "
                "account can only hold ONE citizen profile, so no new ID is "
                "created.\n\n" + profile
            )
            return

        # Start the guided flow: a message with a single Start button.
        message = await ctx.send(
            "📋 **DELTA CITY REGISTRATION**\n"
            "Press **Start Registration** below to begin.",
            view=_StartView(self),
        )
        self._new_session(ctx.guild.id, ctx.author.id, message)

    @register.error
    async def register_error(self, ctx: commands.Context, error: Exception):
        await self._safe_error(ctx, error)

    # ------------------------------------------------------------------
    # step screens (each one edits the same ephemeral message)
    # ------------------------------------------------------------------

    def gender_view(self, session: dict) -> "_StepView":
        options = [discord.SelectOption(label=g, value=g) for g in settings.GENDER_OPTIONS]
        return _StepView(self, session, "gender", "Select your gender.", options)

    def state_view(self, session: dict) -> "_StepView":
        options = [discord.SelectOption(label=s, value=s) for s in settings.STATES]
        return _StepView(self, session, "state", "Which state will you reside in?", options)

    def status_view(self, session: dict) -> "_StepView":
        options = [discord.SelectOption(label=s, value=s) for s in settings.CITIZEN_STATUSES]
        return _StepView(self, session, "status", "What is your status in Delta City?", options)

    def confirm_view(self, session: dict) -> "_ConfirmView":
        return _ConfirmView(self, session)

    # ------------------------------------------------------------------
    # finishing the flow
    # ------------------------------------------------------------------

    async def _finish_registration(
        self, session: dict, guild: discord.Guild, member: discord.Member
    ):
        name = session["name"]
        gender = session["gender"]
        state = session["state"]
        status = session["status"]

        row, created = self.db.register_citizen(member.id, name, gender, state, status)
        if not created:
            # Should be impossible (we checked duplicates at the start), but
            # never mint a second ID.
            await session["message"].edit(
                content=(
                    "You already have a citizen profile, so no new record "
                    "was created."
                ),
                view=None,
            )
            return

        citizen = row
        # Assign the state role so the member is visible in their state.
        try:
            role_name = state  # role names are the bare state name
            role = await helpers.get_or_create_role(guild, role_name)
            hierarchy_err = helpers.hierarchy_error(guild, role)
            if hierarchy_err:
                await session["message"].edit(
                    content=self._format_success(citizen, role_note=hierarchy_err),
                    view=None,
                )
                return
            await member.add_roles(role, reason="Delta City registration")
            await session["message"].edit(
                content=self._format_success(citizen, role_note=None),
                view=None,
            )
        except discord.Forbidden:
            await session["message"].edit(
                content=self._format_success(
                    citizen,
                    role_note=(
                        "⚠️ I could not assign your state role (my role must be "
                        "above it in Server Settings -> Roles). Your "
                        "registration itself is complete."
                    ),
                ),
                view=None,
            )

    def _format_success(self, citizen: dict, role_note):
        note = ("\n\n" + role_note) if role_note else ""
        return (
            "🏙️ **DELTA CITY REGISTRATION COMPLETE**\n"
            + self._format_profile(citizen)
            + note
        )

    @staticmethod
    def _format_profile(citizen: dict) -> str:
        return (
            "**DELTA CITY CITIZEN PROFILE**\n"
            f"Citizen ID: `{citizen['citizen_id']}`\n"
            f"Name: {citizen['name']}\n"
            f"Gender: {citizen['gender']}\n"
            f"State: {citizen['state']}\n"
            f"Status: {citizen['status']}\n"
            f"Registered: {helpers.format_date(citizen['joined_at'])}"
        )

    # ------------------------------------------------------------------
    # shared error handler
    # ------------------------------------------------------------------

    async def _safe_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandNotFound):
            await ctx.send("Unknown command. Try `!dchelp` for the list of Delta City commands.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Missing information — try again, e.g. `!register`.")
            return
        # Never leak a Python traceback to regular users.
        await ctx.send(
            "Something went wrong processing that. Please try again, "
            "or ask an administrator."
        )

    # ------------------------------------------------------------------
    # cog lifecycle
    # ------------------------------------------------------------------

    async def cog_destroy(self):
        self.sessions.clear()


# ----------------------------------------------------------------------
# UI components
# ----------------------------------------------------------------------

class _StartView(discord.ui.View):
    """First screen: a single button that opens the name modal."""

    def __init__(self, cog: RegistrationCog):
        super().__init__(timeout=REGISTRATION_TIMEOUT_SECONDS)
        self._cog = cog

    @discord.ui.button(
        label="Start Registration",
        style=discord.ButtonStyle.primary,
        emoji="📋",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Show the name modal as the interaction response.
        await interaction.response.send_modal(NameModal(self._cog))


class _StepView(discord.ui.View):
    """A single select-menu screen (gender / state / status)."""

    def __init__(self, cog, session, field, prompt, options):
        super().__init__(timeout=REGISTRATION_TIMEOUT_SECONDS)
        self._cog = cog
        self._session = session
        self._field = field
        self._prompt = prompt
        self.select = discord.ui.Select(options=options, placeholder="Choose...")
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        session = self._session
        if not self._cog._session_alive(session):
            await interaction.response.send_message(
                "That session expired — run `!register` to start over.",
                ephemeral=True,
            )
            return
        value = self.select.values[0]
        session[self._field] = value

        if self._field == "gender":
            await interaction.response.edit_message(
                content=self._next_prompt("state"),
                view=self._cog.state_view(session),
            )
        elif self._field == "state":
            await interaction.response.edit_message(
                content=self._next_prompt("status"),
                view=self._cog.status_view(session),
            )
        else:  # status -> show the confirmation screen
            await interaction.response.edit_message(
                content=self._summary(session),
                view=self._cog.confirm_view(session),
            )

    def _next_prompt(self, upcoming: str) -> str:
        questions = {
            "state": "**Which state will you reside in?**",
            "status": "**What is your status in Delta City?**",
        }
        return questions[upcoming]

    def _summary(self, session: dict) -> str:
        return (
            "**DELTA CITY REGISTRATION**\n\n"
            f"Citizen Name: {session['name']}\n"
            f"Gender: {session['gender']}\n"
            f"State: {session['state']}\n"
            f"Status: {session['status']}\n\n"
            "**Is this information correct?**"
        )


class _ConfirmView(discord.ui.View):
    """Final screen: Confirm / Start Over / Cancel buttons.

    Only a **Confirm** press creates the permanent citizen record.
    """

    def __init__(self, cog: RegistrationCog, session: dict):
        super().__init__(timeout=REGISTRATION_TIMEOUT_SECONDS)
        self._cog = cog
        self._session = session
        self._confirm = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.success, emoji="✅"
        )
        self._restart = discord.ui.Button(
            label="Start Over", style=discord.ButtonStyle.primary, emoji="🔄"
        )
        self._cancel = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, emoji="❌"
        )
        self._confirm.callback = self._on_confirm
        self._restart.callback = self._on_restart
        self._cancel.callback = self._on_cancel
        self.add_item(self._confirm)
        self.add_item(self._restart)
        self.add_item(self._cancel)

    async def _on_confirm(self, interaction: discord.Interaction):
        session = self._session
        if not self._cog._session_alive(session):
            await interaction.response.send_message(
                "That registration session has expired. "
                "Run `!register` to start over.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "You must be a member of the server to finish registration.",
                ephemeral=True,
            )
            return
        # Only an explicit Confirm press creates the permanent record.
        await self._cog._finish_registration(
            session, interaction.guild, interaction.user
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ Registration complete — your profile is now in the city registry.",
            view=self,
        )

    async def _on_restart(self, interaction: discord.Interaction):
        session = self._session
        if not self._cog._session_alive(session):
            await interaction.response.send_message(
                "That registration session has expired. "
                "Run `!register` to start over.",
                ephemeral=True,
            )
            return
        # Clear every answer and go back to the very first screen.
        session["name"] = None
        session["gender"] = None
        session["state"] = None
        session["status"] = None
        session["started_at"] = time.time()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                "📋 **DELTA CITY REGISTRATION**\n"
                "Press **Start Registration** below to begin again."
            ),
            view=_StartView(self._cog),
        )

    async def _on_cancel(self, interaction: discord.Interaction):
        session = self._session
        # Cancelling never creates a record and never reserves a number.
        self._cog._kill_session(interaction.guild_id, interaction.user.id)
        self._cog._log_audit(
            action="registration_cancelled",
            subject_id=interaction.user.id,
            details="Registration started then cancelled; no record created",
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                "❌ Registration cancelled. No record was created and no "
                "citizen number was reserved. You can start again any time "
                "with `!register`."
            ),
            view=self,
        )


async def setup(bot: commands.Bot):
    """Called by discord.py when the cog is loaded from bot.py."""
    await bot.add_cog(RegistrationCog(bot))