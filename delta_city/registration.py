"""DELTA CITY immigration state machine (delta_city/registration.py).

The heart of the arrival/registration experience.  The module is split
into two clearly separated halves:

1.  Pure state-machine logic (no Discord imports):
    session lifecycle, stage transitions, selection validation and the
    progressive message copy.  Every transition operates on a plain
    session dict in the shape of the ``registration_sessions`` table row,
    so the whole flow is unit-testable without a bot connection.

2.  The Discord UI (``ImmigrationView``):
    a *persistent* ``discord.ui.View`` whose children are declared with
    item decorators and fixed custom ids, so discord.py can route
    interactions on stale messages back to a single shared instance
    registered via ``bot.add_view`` at startup (see ``setup`` below).
    The session in SQLite is the source of truth: on every interaction
    the handler re-loads the session,
    re-applies stage state (which select is live, which buttons are
    enabled), and re-renders the message copy.  Because the DB row is
    re-validated on every click, a restart never loses progress and a
    stale message can never assign a role or reserve a Citizen ID.

Stages: ``city`` -> ``community`` -> ``gender`` -> ``review`` -> done.
A session only becomes a permanent citizen record after the user
presses *Confirm* on the review stage; cancel or timeout discards the
session without creating a record or reserving a number.

Self-service check-in (`!check-in`, Asylum role) leads through the Begin button, a registration modal (character name / DOB / gender / city), a home-state select, and an officer approve/reject that issues the citizen record plus birth certificate.  Staff toggle roles with the hidden `!role` command and per-role shorthand `!RoleName @user` commands registered at cog setup.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

import discord
from discord.ext import commands

from config import settings
from database.database import DeltaCityDB
from delta_city import identity
from delta_city import id_card, portrait
from delta_city import permissions

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# stage model
# ---------------------------------------------------------------------------

#: Session lifetime for an in-progress registration (seconds).
REGISTRATION_TIMEOUT = 600

#: Ordered stages of the immigration flow.  ``review`` is the last stage
#: with a select-less screen (confirm / regenerate / cancel buttons).
STAGES: tuple[str, ...] = ("city", "community", "gender", "review")

#: Stage -> ("Step N of 3", prompt line) for the progressive copy.
STAGE_LABELS: dict[str, tuple[str, str]] = {
    "city": ("Step 1 of 3", "Select your city."),
    "community": ("Step 2 of 3", "Select your community / heritage."),
    "gender": ("Step 3 of 3", "Select your gender."),
}

#: Nationality stamped on every Delta City citizen.
NATIONALITY = "Deltaian"

#: How long an in-progress session stays resumable (hours).
SESSION_TIMEOUT_HOURS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_expiry(now: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp SESSION_TIMEOUT_HOURS from *now*."""
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now + timedelta(hours=SESSION_TIMEOUT_HOURS)).isoformat()


def parse_expiry(value: str | None) -> datetime | None:
    """Parse an expiry timestamp; naive values are treated as UTC."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def session_stage(session: Mapping[str, Any] | None) -> str:
    """Current stage of *session* (falls back to ``city``)."""
    if not session:
        return "city"
    stage = session.get("stage") or "city"
    return stage if stage in STAGES else "city"


def is_expired(session: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
    """True when *session* has passed its expiry (or carries none)."""
    if not session:
        return True
    stamp = parse_expiry(session.get("expires_at"))
    if stamp is None:
        return True
    return stamp <= (now or _utcnow())


def stage_options(stage: str, session: Mapping[str, Any] | None = None) -> list[str]:
    """Valid choices at *stage* (for select menus and validation)."""
    if stage == "city":
        return list(identity.CITY_PREFIXES)
    if stage == "community":
        city = (session or {}).get("city")
        if not city or city not in identity.CITY_PREFIXES:
            return []
        return list(identity.CITY_COMMUNITIES.get(city, []))
    if stage == "gender":
        return list(identity.GENDER_OPTIONS)
    return []


def apply_selection(
    session: Mapping[str, Any] | None, value: str
) -> tuple[dict[str, Any], str]:
    """Validate *value* against the session's current stage.

    Returns ``(updated_session_dict, next_stage)``.  Raises
    ``ValueError`` for an empty/invalid choice or a session that is not
    at a selectable stage.
    """
    if session is None:
        raise ValueError("No registration session is open.")
    stage = session_stage(session)
    if stage == "review":
        raise ValueError("Registration is already complete; select nothing more.")

    choice = str(value or "").strip()
    options = stage_options(stage, session)
    if choice not in options:
        raise ValueError(f"Invalid choice for {stage} stage: {value!r}")

    updated: dict[str, Any] = dict(session)
    if stage == "city":
        updated["city"] = choice
        # A new city invalidates a previously picked community.
        updated["community"] = None
    elif stage == "community":
        updated["community"] = choice
    elif stage == "gender":
        updated["gender"] = choice
    updated["stage"] = STAGES[STAGES.index(stage) + 1]
    return updated, STAGES[STAGES.index(stage) + 1]


def is_complete(session: Mapping[str, Any] | None) -> bool:
    """All three answers collected (session at the review stage)."""
    if not session:
        return False
    return (
        session.get("city") in identity.CITY_PREFIXES
        and identity.is_community(session.get("city") or "", session.get("community") or "")
        and session.get("gender") in identity.GENDER_OPTIONS
    )


# ---------------------------------------------------------------------------
# message copy (rendered by the view; pure strings, unit-testable)
# ---------------------------------------------------------------------------

WELCOME_BORDER = (
    "🛬 **WELCOME TO DELTA CITY**\n\n"
    "You have arrived at the Delta City border.\n\n"
    "Before you may enter the city, your identity must be registered."
)


def render_intro(session: Mapping[str, Any] | None = None) -> str:
    """Step-1 copy: welcome banner + city prompt."""
    label, prompt = STAGE_LABELS["city"]
    if session and session.get("city"):
        label = ""  # already past the city question — keep the banner only
    header = WELCOME_BORDER
    if label:
        header += f"\n\n{label}\n{prompt}"
    return header


def _confirmed_lines(session: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if session.get("city"):
        lines.append(f"✅ City selected: {session['city']}")
    if session.get("community"):
        lines.append(f"✅ Community selected: {session['community']}")
    if session.get("gender"):
        lines.append(f"✅ Gender selected: {session['gender']}")
    return lines


def render_step_content(session: Mapping[str, Any]) -> str:
    """Progressive copy for whatever stage the session is currently at."""
    stage = session_stage(session)
    lines = [WELCOME_BORDER]
    confirmed = _confirmed_lines(session)
    if confirmed:
        lines.append("\n" + "\n".join(confirmed))
    if stage in STAGE_LABELS:
        label, prompt = STAGE_LABELS[stage]
        lines.append(f"\n{label}\n{prompt}")
    return "\n".join(lines)


def render_review_content(session: Mapping[str, Any], generated_name: str | None) -> str:
    """Review stage copy: generated identity + confirm/regenerate/cancel."""
    name = generated_name or "—"
    return (
        WELCOME_BORDER
        + "\n\n"
        + "\n".join(_confirmed_lines(session))
        + "\n\n**Your Delta City identity has been generated:**\n\n"
        f"Name: **{name}**\n"
        f"City: {session.get('city')}\n"
        f"Community: {session.get('community')}\n"
        f"Gender: {session.get('gender')}\n"
        f"Nationality: {NATIONALITY}\n\n"
        "🪪 *This is a fictional Delta City identity for the server "
        "experience — it does not represent your real-world identity.*\n\n"
        "Press **Confirm** to finalize your registration, "
        "**Regenerate** to draw a different identity, or **Cancel** to leave."
    )


PROCESSING_STEPS: tuple[tuple[int, str], ...] = (
    (20, "Establishing citizenship record..."),
    (40, "Generating Deltaian identity..."),
    (60, "Preparing identity portrait..."),
    (80, "Issuing Citizen ID..."),
    (100, "Preparing identity card..."),
)


def render_processing(percent: int, step_text: str) -> str:
    """One frame of the IDENTITY PROCESSING animation."""
    bar_len = 10
    filled = max(0, min(bar_len, round(bar_len * percent / 100)))
    bar = "▓" * filled + "░" * (bar_len - filled)
    return (
        "⏳ **IDENTITY PROCESSING**\n\n"
        f"{step_text}\n"
        f"{bar} {percent}%"
    )


def render_complete_content(record: Mapping[str, Any]) -> str:
    """Final frame after registration is finalized."""
    return (
        "✅ **REGISTRATION COMPLETE**\n\n"
        "🇩🇨 **WELCOME TO DELTA CITY**\n\n"
        f"Name: {record.get('name')}\n"
        f"Citizen ID: `{record.get('citizen_id')}`\n"
        f"State/City: {record.get('city') or record.get('state')}\n"
        f"Community: {record.get('community')}\n"
        f"Gender: {record.get('gender')}\n"
        f"Nationality: {record.get('nationality') or NATIONALITY}\n"
        f"Status: **{record.get('status', 'ACTIVE')}**\n\n"
        "Your Citizen Identity Card is being issued below."
    )


def render_arrival_announcement(record: Mapping[str, Any]) -> str:
    """Public announcement posted in #arrival-station after registration."""
    return (
        "🛬 **NEW CITIZEN ARRIVAL**\n\n"
        "A new citizen has successfully entered Delta City.\n\n"
        f"NAME:\n{record.get('name')}\n\n"
        f"CITIZEN ID:\n`{record.get('citizen_id')}`\n\n"
        f"CITY:\n{record.get('city') or record.get('state')}\n\n"
        f"COMMUNITY:\n{record.get('community')}\n\n"
        "STATUS:\n🟢 **ACTIVE**\n\n"
        "Welcome to Delta City."
    )


def render_new_arrival(member: Any) -> str:
    """Announcement when an *unregistered* user first enters the server."""
    mention = getattr(member, "mention", None) or str(member)
    return (
        "🛬 **NEW ARRIVAL**\n\n"
        f"{mention} A new arrival has entered Delta City.\n\n"
        "Citizen registration is required before entry into the city can be granted.\n\n"
        "Use the menu below to begin your immigration assessment."
    )


def render_already_registered(citizen: Mapping[str, Any]) -> str:
    """Copy shown instead of a second registration (existing citizens)."""
    return (
        "🇩🇨 You are already registered as a Delta City citizen.\n\n"
        f"Citizen ID:\n`{citizen.get('citizen_id')}`\n\n"
        "No second identity will be issued. Your existing record is being used."
    )


def build_arrival_message(
    user_mention: str,
    year: int,
    rules_channel_id: str,
    citizenship_channel_id: str,
    officer_role_id: str,
) -> str:
    """Flight-style arrival announcement posted in the airport channel."""
    return (
        "✈️ **FLIGHT ARRIVAL: NOW LANDING IN DELTA CITY**\n\n"
        f"Please welcome {user_mention}! 🎉\n"
        f"Flight **DC-{year}** has touched down at Delta International Airport.\n\n"
        "🏙️ **WELCOME TO THE CITY OF LIFE!**\n\n"
        "You're not through the gates just yet. Before you step out into "
        "the Metropolis, complete your arrival checklist:\n\n"
        f"**1.** Read the city regulations in <#{rules_channel_id}>\n"
        f"**2.** Apply for citizenship in <#{citizenship_channel_id}> "
        "with `!check-in`\n"
        "**3.** Wait at arrivals for an immigration officer to clear you\n\n"
        "🛂 **Status:** Asylum, awaiting clearance\n\n"
        "Mind your luggage, watch your step, and enjoy your stay in DC.\n\n"
        f"<@&{officer_role_id}> a new arrival is waiting. Please direct "
        "them through immigration when you get a moment."
    )


def build_departure_message(name: str) -> str:
    """Flight-style departure announcement; name is escaped plain text."""
    safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
    return (
        "🛫 **FLIGHT DEPARTURE: NOW BOARDING OUT OF DELTA CITY**\n\n"
        f"{safe_name} has boarded an exit flight and left Delta City.\n\n"
        "Thank you for spending time in the City of Life. The runway is "
        "always open if you decide to come back. 🏙️\n\n"
        "Safe travels, you'll be missed 🥺"
    )


def render_session_expired() -> str:
    """Copy for a stale/abandoned session; invites a fresh start."""
    return (
        "⌛ **SESSION EXPIRED**\n\n"
        "Your immigration assessment has expired. No record was created "
        "and no Citizen ID was reserved.\n\n"
        "Use `!arrival` to start a new registration."
    )


def render_cancelled() -> str:
    return (
        "❌ **REGISTRATION CANCELLED**\n\n"
        "No record was created and no Citizen ID was reserved. "
        "You may begin again any time with `!arrival`."
    )


def render_processing_failed(reason: str) -> str:
    return (
        "⚠️ **PROCESSING PROBLEM**\n\n"
        f"Something went wrong while preparing your identity: {reason}\n\n"
        "Your registration is still pending — your choices were saved. "
        "Press **Retry** to continue, or **Cancel** to abandon the assessment."
    )


# ---------------------------------------------------------------------------
# Discord UI: persistent immigration view
# ---------------------------------------------------------------------------

_CUSTOM_ID = "dcim:{user_id}"
_DEFAULT_PLACEHOLDER = "Select..."
_DISABLED_NOTE = "This option is not available for your current step."


def _select_options(values: list[str]) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=value, value=value, emoji="🏙️")
        for value in values
    ]


class ImmigrationView(discord.ui.View):
    """Persistent view driving the whole arrival/registration flow.

    Children are declared with the ``@discord.ui.select`` /
    ``@discord.ui.button`` decorators (bare class-level ``Select`` /
    ``Button`` instances are silently dropped by discord.py's
    ``View.__init_subclass__``) with fixed ``custom_id``s, so discord.py
    can reconstruct the view after a bot restart without constructor
    arguments.  ``_apply_stage`` (re)enables the correct children for the
    session's current stage and re-renders the message copy.

    The view never trusts its own in-memory state: every interaction
    re-reads the session from the database, validates the choice against
    the current stage, and only persists the new stage on success.  A
    stale message (e.g. from before a restart) therefore degrades to an
    error notice instead of corrupting data.
    """

    # -- persistent children (decorator-style; fixed custom ids so the
    #    view can be reconstructed from a stored message after a restart) --

    @discord.ui.select(
        placeholder=_DEFAULT_PLACEHOLDER,
        options=_select_options(list(identity.CITY_PREFIXES)),
        custom_id="dcim:city",
    )
    async def city_select(self, interaction: discord.Interaction) -> None:
        await self._handle_selection(interaction, self.city_select.value)

    @discord.ui.select(
        placeholder=_DEFAULT_PLACEHOLDER,
        options=[],
        custom_id="dcim:community",
    )
    async def community_select(self, interaction: discord.Interaction) -> None:
        await self._handle_selection(interaction, self.community_select.value)

    @discord.ui.select(
        placeholder=_DEFAULT_PLACEHOLDER,
        options=_select_options(list(identity.GENDER_OPTIONS)),
        custom_id="dcim:gender",
    )
    async def gender_select(self, interaction: discord.Interaction) -> None:
        await self._handle_selection(interaction, self.gender_select.value)

    @discord.ui.button(
        label="Confirm",
        emoji="✅",
        custom_id="dcim:confirm",
        style=discord.ButtonStyle.success,
    )
    async def confirm_button(self, interaction: discord.Interaction) -> None:
        await self._finalize(interaction, respond=True)

    @discord.ui.button(
        label="Regenerate",
        emoji="🔄",
        custom_id="dcim:regenerate",
        style=discord.ButtonStyle.secondary,
    )
    async def regenerate_button(self, interaction: discord.Interaction) -> None:
        await self._regenerate(interaction)

    @discord.ui.button(
        label="Cancel",
        emoji="❌",
        custom_id="dcim:cancel",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_button(self, interaction: discord.Interaction) -> None:
        await self._cancel(interaction)

    @discord.ui.button(
        label="Retry",
        emoji="🔁",
        custom_id="dcim:retry",
        style=discord.ButtonStyle.success,
    )
    async def retry_button(self, interaction: discord.Interaction) -> None:
        await self._retry(interaction)

    def __init__(
        self,
        user_id: int,
        cog: "ImmigrationCog | None" = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(timeout=None)  # persistent; expiry enforced via DB
        self.user_id = int(user_id)
        self._cog = cog
        self._session: dict[str, Any] | None = dict(session) if session else None
        if session:
            self._apply_stage()

    # -- session plumbing --------------------------------------------------

    def _db(self):
        return self._cog.bot.db if self._cog else None

    def _load_session(self) -> Mapping[str, Any] | None:
        """Re-read the authoritative session from the database.

        After a bot restart the in-memory view instance is fresh, so the
        DB copy is always the source of truth.
        """
        db = self._db()
        if db is None:
            return self._session
        return db.get_registration_session(self.user_id)

    def _bind(self, interaction: discord.Interaction) -> None:
        """Re-attach per-user/cog state after a bot restart.

        After a restart the in-memory per-message view registrations are
        gone, so discord.py routes interactions on stale messages to the
        single shared persistent instance registered in ``setup`` via
        ``bot.add_view``.  That instance carries no user context, so the
        interaction itself is the source of truth: re-bind ``user_id``
        and the cog from it.
        """
        if self.user_id != interaction.user.id:
            self.user_id = int(interaction.user.id)
        if self._cog is None:
            get_cog = getattr(interaction.client, "get_cog", None)
            if get_cog is not None:
                self._cog = get_cog("Immigration")

    def _apply_stage(self) -> None:
        """(Re)enable the children for the current stage and refresh copy."""
        session = self._session or {}
        stage = session_stage(session)
        self.city_select.disabled = stage != "city"
        self.gender_select.disabled = stage != "gender"
        self.community_select.disabled = stage != "community"
        if stage == "community":
            self.community_select.options = _select_options(
                stage_options("community", session)
            )
        # Buttons appear only on the review stage.
        for button in (self.confirm_button, self.regenerate_button, self.cancel_button):
            button.disabled = stage != "review"

    async def _handle_selection(
        self, interaction: discord.Interaction, value: str
    ) -> None:
        self._bind(interaction)
        session = self._load_session()
        if is_expired(session):
            db = self._db()
            if db:
                db.delete_registration_session(self.user_id)
            await interaction.response.edit_message(
                content=render_session_expired(), view=None
            )
            return
        try:
            updated, next_stage = apply_selection(session, value)
        except ValueError as exc:
            log.info("Invalid selection for %s: %s", self.user_id, exc)
            await interaction.response.send_message(
                f"⚠️ {exc}", ephemeral=True
            )
            return

        db = self._db()
        if db:
            db.update_registration_session(
                self.user_id,
                stage=updated["stage"],
                city=updated.get("city"),
                community=updated.get("community"),
                gender=updated.get("gender"),
            )
        self._session = updated
        self._apply_stage()
        try:
            await interaction.response.edit_message(
                content=render_step_content(updated), view=self
            )
        except discord.HTTPException:
            # The original message may have been deleted; fall back to a reply.
            await interaction.response.send_message(
                f"⚠️ I can't find my earlier message for you. "
                f"Use `!arrival` to restart your registration.", ephemeral=True
            )

    async def _retry(self, interaction: discord.Interaction) -> None:
        await self._finalize(interaction, respond=True)

    async def _regenerate(self, interaction: discord.Interaction) -> None:
        self._bind(interaction)
        session = self._load_session()
        if is_expired(session):
            db = self._db()
            if db:
                db.delete_registration_session(self.user_id)
            await interaction.response.edit_message(
                content=render_session_expired(), view=None
            )
            return
        community = (session or {}).get("community") or ""
        gender = (session or {}).get("gender") or ""
        if not community or not gender:
            await interaction.response.send_message(
                "⚠️ Answer every question before regenerating.", ephemeral=True
            )
            return
        new_name = identity.generate_name(community, gender)
        db = self._db()
        if db:
            db.update_registration_session(self.user_id, generated_name=new_name)
        self._session = dict(session)
        self._session["generated_name"] = new_name
        try:
            await interaction.response.edit_message(
                content=render_review_content(self._session, new_name), view=self
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                f"⚠️ I can't find my earlier message for you. "
                f"Use `!arrival` to restart your registration.", ephemeral=True
            )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self._bind(interaction)
        db = self._db()
        if db:
            db.delete_registration_session(self.user_id)
        self._session = None
        await interaction.response.edit_message(
            content=render_cancelled(), view=None
        )

    # -- finalization -------------------------------------------------------

    async def _finalize(self, interaction: discord.Interaction, *, respond: bool) -> None:
        """Run identity generation, issue the record, assign roles, announce."""
        self._bind(interaction)
        user_id = self.user_id  # capture before any await; the shared instance may be rebound
        session = self._load_session()
        if is_expired(session) or not is_complete(session):
            db = self._db()
            if db:
                db.delete_registration_session(self.user_id)
            await interaction.response.edit_message(
                content=render_session_expired()
                if is_expired(session)
                else "⚠️ Finish every step before confirming.",
                view=None,
            )
            return

        # Duplicate guard: a record must never be issued twice.
        db = self._db()
        existing = db.get_citizen(self.user_id) if db else None
        if existing is not None:
            self._session = None
            try:
                await interaction.response.edit_message(
                    content=render_already_registered(existing), view=None
                )
            except discord.HTTPException:
                await interaction.response.send_message(
                    render_already_registered(existing), ephemeral=True
                )
            return

        city = session["city"]
        community = session["community"]
        gender = session["gender"]
        name = (
            session.get("generated_name")
            or identity.generate_name(community, gender)
        )

        if respond:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        try:
            await self._run_processing(interaction, user_id, city, community, gender, name, db)
        except Exception as exc:  # noqa: BLE001 - registration must never die silently
            log.exception("Registration processing failed for %s", user_id)
            # The instance may now be bound to another user's in-flight
            # interaction; never wipe the shared instance in that case.
            still_ours = self.user_id == user_id
            if still_ours:
                self.clear_items()
                self.retry_button.disabled = False
                self.cancel_button.disabled = False
            try:
                await interaction.followup.send(
                    render_processing_failed(str(exc)[:200]),
                    view=self if still_ours else None,
                )
            except discord.HTTPException:
                pass

    async def _run_processing(
        self,
        interaction: discord.Interaction,
        user_id: int,
        city: str,
        community: str,
        gender: str,
        name: str,
        db,
    ) -> None:
        """Animated processing sequence ending in a fully issued record.

        ``user_id`` is captured before any await by the caller, because
        the shared persistent instance may be rebound to another user's
        interaction while this coroutine is suspended.
        """
        if self._db() is None:
            raise RuntimeError("Registration database unavailable.")

        steps = [
            (20, "Establishing citizenship record..."),
            (40, "Generating Deltaian identity..."),
            (60, "Preparing identity portrait..."),
            (80, "Issuing Citizen ID..."),
            (100, "Preparing identity card..."),
        ]
        for percent, step_text in steps:
            await interaction.followup.send(
                render_processing(percent, step_text), ephemeral=True, wait=True
            )
            await asyncio.sleep(0.8)

        # 1) Issue the record (sequential, unique Citizen ID inside a
        #    transaction; never a second identity for the same account).
        record, created = db.register_citizen(
            user_id, name=name, gender=gender, state=city,
            community=community, nationality=NATIONALITY,
        )
        if not created:
            self._session = None
            await interaction.followup.send(
                render_already_registered(record), ephemeral=True, wait=True
            )
            return
        citizen_id = record["citizen_id"]

        # 2) Portrait + ID card. Failures keep the record pending so a
        #    retry can attach the documents without re-issuing an ID.
        portrait_png = b""
        card_png = b""
        try:
            portrait_png = portrait.generate_portrait(citizen_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Portrait generation failed for %s: %s", citizen_id, exc)
        if portrait_png:
            try:
                card_png = id_card.render_id_card(record, portrait_png)
            except Exception as exc:  # noqa: BLE001
                log.warning("ID card generation failed for %s: %s", citizen_id, exc)
        if card_png:
            paths = _store_citizen_files(db, citizen_id, portrait_png, card_png)
            if paths:
                db.attach_citizen_files(user_id, **paths)

        # 3) Close the session — the identity is now permanent.
        db.delete_registration_session(user_id)
        self._session = None

        # 4) Roles: 🇩🇨 Deltaian (national) + the city citizen role.
        guild = interaction.guild
        if guild is not None:
            try:
                await self._assign_citizen_roles(guild, interaction.user, city)
            except discord.HTTPException as exc:
                log.warning("Role assignment failed for %s: %s", citizen_id, exc)

        # 5) Reveal the result in the arrival message.
        try:
            await interaction.original_response.edit(
                content=render_complete_content(record), view=None
            )
        except discord.HTTPException:
            pass

        # 6) Public arrival announcement with the ID card attached.
        if guild is not None and card_png:
            try:
                await self._announce_arrival(guild, record, card_png)
            except discord.HTTPException as exc:
                log.warning("Arrival announcement failed: %s", exc)

    # -- guild helpers ------------------------------------------------------

    async def _assign_citizen_roles(
        self, guild: discord.Guild, member: discord.Member, city: str
    ) -> None:
        """Grant the national role plus the selected city's citizen role.

        Enforces the one-state-at-a-time rule: any citizen role from a
        *different* city is stripped first (a member is either a citizen
        of one state or a visitor of another).  The Unverified role is
        removed as well.  Granting *city*'s citizen role is what unlocks
        the member's state category —
        :func:`delta_city.permissions.overrides_for_category` allows
        exactly those roles.
        """
        added = []
        deltaian = self._find_role(guild, "Deltaian")
        if deltaian is not None and deltaian not in member.roles:
            await member.add_roles(deltaian, reason="Delta City registration")
            added.append(deltaian.name)

        # Exclusivity: a citizen holds exactly one state's citizenship.
        stripped = []
        for other_city in identity.CITY_PREFIXES:
            if other_city.lower() == city.lower():
                continue
            for role_name in permissions.city_role_names(other_city):
                role = discord.utils.get(guild.roles, name=role_name)
                if role is not None and role in member.roles:
                    await member.remove_roles(
                        role, reason=f"Citizenship moved to {city}"
                    )
                    stripped.append(role.name)

        city_role = permissions.find_city_role(guild, city)
        if city_role is not None and city_role not in member.roles:
            await member.add_roles(
                city_role, reason=f"Delta City registration ({city})"
            )
            added.append(city_role.name)

        unverified = await permissions.remove_unverified_role(member)
        if unverified is not None:
            stripped.append(unverified.name)

        if added:
            log.info("Assigned %s to %s (%s)", ", ".join(added), member.id, city)
        if stripped:
            log.info("Removed %s from %s (%s)", ", ".join(stripped), member.id, city)

    @staticmethod
    def _find_role(guild: discord.Guild, fragment: str):
        import unicodedata

        for role in guild.roles:
            name = unicodedata.normalize("NFKC", role.name or "")
            if fragment in name:
                return role
        return None

    async def _announce_arrival(
        self, guild: discord.Guild, record: Mapping[str, Any], card_png: bytes
    ) -> None:
        channel = None
        for ch in guild.text_channels:
            if permissions.is_arrival_channel(ch.name):
                channel = ch
                break
        if channel is None:
            log.warning("No arrival-station channel found; skipping announcement.")
            return
        embed = discord.Embed(
            description=render_arrival_announcement(record),
            color=discord.Color.green(),
        )
        file = discord.File(
            io.BytesIO(card_png), filename=f"{record['citizen_id']}.png"
        )
        await channel.send(embed=embed, file=file)


# ---------------------------------------------------------------------------
# Shared renderers
# ---------------------------------------------------------------------------


def render_arrival_header(member: discord.Member) -> str:
    """Step-1 header shown when the immigration form is first posted."""
    return (
        "🛬 **WELCOME TO DELTA CITY**\n\n"
        f"{member.mention} — you have arrived at the Delta City border.\n\n"
        "Before you may enter the city, your identity must be registered. "
        "This whole process takes under a minute.\n\n"
        "**Step 1 of 3 — Select your city.**"
    )


def render_citizen_profile(record: Mapping[str, Any]) -> str:
    """Public-safe citizen profile (no Discord user IDs)."""
    lines = [
        f"🇩🇨 **DELTA CITY — CITIZEN RECORD**",
        "",
        f"**NAME:** {record.get('name') or '—'}",
        f"**CITIZEN ID:** `{record.get('citizen_id') or '—'}`",
        f"**CITY:** {record.get('state') or '—'}",
        f"**COMMUNITY:** {record.get('community') or '—'}",
        f"**GENDER:** {record.get('gender') or '—'}",
        f"**NATIONALITY:** {record.get('nationality') or 'Deltaian'}",
        f"**STATUS:** {record.get('status') or '—'}",
        f"**REGISTERED:** {record.get('joined_at') or '—'}",
    ]
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Field limits and persistent interaction ids
# ---------------------------------------------------------------------------

MAX_NAME_LEN = 100
MAX_DOB_LEN = 32
MAX_GENDER_LEN = 50
MAX_CITY_LEN = 100

# Persistent (survive restarts via bot.add_view) interaction custom ids.
BEGIN_BUTTON_ID = "dcim:begin"
CANCEL_RESTART_BUTTON_ID = "dcim:cancel-restart"
STATE_SELECT_ID = "dcim:state"
APPROVE_BUTTON_ID = "dcim:approve"
REJECT_BUTTON_ID = "dcim:reject"

# Transient (per-invocation) interaction ids.
ROLE_SELECT_ID = "dcim:role"

MODAL_TITLE = "Delta City — Check-In Registration"
MODAL_PLACEHOLDERS = {
    "character_name": "Your character's full name",
    "date_of_birth": "e.g. 12/05/1994",
    "gender": "e.g. Male / Female / Non-binary",
    "city": "Your home city on Earth",
}

PENDING_NOTICE = "You already have an application pending."


# ---------------------------------------------------------------------------
# Errors and pure helpers (no Discord objects required — unit-testable)
# ---------------------------------------------------------------------------


class RegistrationError(Exception):
    """User-facing validation error for the check-in flow."""


def _clean(value: Any, max_len: int) -> str:
    """Strip, collapse internal whitespace and cap length."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:max_len]


def sanitize_modal_fields(
    character_name: str,
    date_of_birth: str,
    gender: str,
    city: str,
) -> dict:
    """Clean the four modal inputs; raise RegistrationError when a
    required field is empty.  Date of birth is optional and stored as
    None when blank.  Returns a dict with keys character_name,
    date_of_birth, gender, city."""
    name = _clean(character_name, MAX_NAME_LEN)
    dob = _clean(date_of_birth, MAX_DOB_LEN)
    gender_clean = _clean(gender, MAX_GENDER_LEN)
    city_clean = _clean(city, MAX_CITY_LEN)

    missing = []
    if not name:
        missing.append("character name")
    if not gender_clean:
        missing.append("gender")
    if not city_clean:
        missing.append("city")
    if missing:
        raise RegistrationError(
            "Please fill in: " + ", ".join(missing) + "."
        )
    return {
        "character_name": name,
        "date_of_birth": dob or None,
        "gender": gender_clean,
        "city": city_clean,
    }


def certificate_number(state: str, number: int) -> str:
    """Numbered certificate id: ``DC-<STATE CODE>-0123`` (4 digits)."""
    code = settings.STATE_CODES[state]
    return f"DC-{code}-{int(number):04d}"


def state_select_options() -> list:
    """The six home-state options for the state select menu."""
    return [
        discord.SelectOption(
            label=state, value=state, description=f"Home state: {state}"
        )
        for state in settings.STATES
    ]


def normalize_role_name(name: str) -> str:
    """Lowercase letters+digits only: '🇩🇨 Deltaian' -> 'deltaian'."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def resolve_shorthand_role(command_name: str, allowed: Iterable[str]) -> Optional[str]:
    """Resolve a ``!RoleName`` shorthand against the actor's allowed roles.

    Exact (normalised) match first, then a unique normalised prefix of at
    least 4 characters.  Returns the matched role name or None.
    """
    wanted = normalize_role_name(command_name)
    if not wanted:
        return None
    allowed_list = list(allowed)
    for role_name in allowed_list:
        if normalize_role_name(role_name) == wanted:
            return role_name
    if len(wanted) >= 4:
        prefix_hits = [
            r for r in allowed_list if normalize_role_name(r).startswith(wanted)
        ]
        if len(prefix_hits) == 1:
            return prefix_hits[0]
    return None


def command_name_for_role(role_name: str) -> str:
    """Discord command name for a role's shorthand command:
    'Citizen of Asaba' -> 'citizen_of_asaba', 'Deltan' -> 'deltan'."""
    name = re.sub(r"[^a-z0-9]+", "_", str(role_name).lower()).strip("_")
    return name or "role"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class CheckInView(discord.ui.View):
    """Persistent 'Begin Registration' button, re-attached on restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Begin Registration",
        custom_id=BEGIN_BUTTON_ID,
        style=discord.ButtonStyle.primary,
        emoji="🛂",
    )
    async def begin(self, interaction: discord.Interaction, button):
        cog = interaction.client.get_cog("ImmigrationCog")
        if cog is None:
            return
        await cog._on_begin_pressed(interaction)


class PendingNoticeView(discord.ui.View):
    """'Cancel and start over' button — no dead ends in the flow."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Cancel and start over",
        custom_id=CANCEL_RESTART_BUTTON_ID,
        style=discord.ButtonStyle.danger,
        emoji="↩️",
    )
    async def cancel_restart(self, interaction: discord.Interaction, button):
        cog = interaction.client.get_cog("ImmigrationCog")
        if cog is None:
            return
        await cog._on_cancel_restart_pressed(interaction)


class RegistrationModal(discord.ui.Modal):
    """Step 1: character name, date of birth, gender, city."""

    def __init__(self):
        super().__init__(title=MODAL_TITLE)
        self.character_name = discord.ui.TextInput(
            label="Character Name",
            custom_id="dcim:name",
            style=discord.TextStyle.short,
            max_length=MAX_NAME_LEN,
            placeholder=MODAL_PLACEHOLDERS["character_name"],
            required=False,
        )
        self.date_of_birth = discord.ui.TextInput(
            label="Date of Birth",
            custom_id="dcim:dob",
            style=discord.TextStyle.short,
            max_length=MAX_DOB_LEN,
            placeholder=MODAL_PLACEHOLDERS["date_of_birth"],
            required=False,
        )
        self.gender = discord.ui.TextInput(
            label="Gender",
            custom_id="dcim:gender",
            style=discord.TextStyle.short,
            max_length=MAX_GENDER_LEN,
            placeholder=MODAL_PLACEHOLDERS["gender"],
            required=False,
        )
        self.city = discord.ui.TextInput(
            label="City",
            custom_id="dcim:city",
            style=discord.TextStyle.short,
            max_length=MAX_CITY_LEN,
            placeholder=MODAL_PLACEHOLDERS["city"],
            required=False,
        )

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("ImmigrationCog")
        if cog is None:
            return
        await cog._on_modal_submitted(
            interaction,
            self.character_name.value or "",
            self.date_of_birth.value or "",
            self.gender.value or "",
            self.city.value or "",
        )


class StateSelectView(discord.ui.View):
    """Step 2: choose the home state (six states)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id=STATE_SELECT_ID,
        placeholder="Choose your home state",
        options=state_select_options(),
    )
    async def state_select(self, interaction: discord.Interaction, select):
        cog = interaction.client.get_cog("ImmigrationCog")
        if cog is None:
            return
        await cog._on_state_selected(interaction, select.values[0])


class ApplicationView(discord.ui.View):
    """Approve / Reject buttons on the application post. Officers only."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Approve",
        custom_id=APPROVE_BUTTON_ID,
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    async def approve(self, interaction: discord.Interaction, button):
        cog = interaction.client.get_cog("ImmigrationCog")
        if cog is None:
            return
        await cog._on_approve_pressed(interaction)

    @discord.ui.button(
        label="Reject",
        custom_id=REJECT_BUTTON_ID,
        style=discord.ButtonStyle.danger,
        emoji="❌",
    )
    async def reject(self, interaction: discord.Interaction, button):
        cog = interaction.client.get_cog("ImmigrationCog")
        if cog is None:
            return
        await cog._on_reject_pressed(interaction)


class RoleSelectView(discord.ui.View):
    """!role dropdown — built per-invoker with only allowed roles."""

    def __init__(
        self,
        cog: "ImmigrationCog",
        invoker: discord.Member,
        target: discord.Member,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.invoker = invoker
        self.target = target
        options = [
            discord.SelectOption(
                label=role_name,
                value=role_name,
                description="Assign or remove this role",
            )
            for role_name in permissions.assignable_role_names(invoker)
            if role_name
        ]
        self.select = discord.ui.Select(
            custom_id=ROLE_SELECT_ID,
            placeholder="Choose a role to assign or remove",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.add_item(self.select)


# ---------------------------------------------------------------------------
# Cog — commands + member join hook
# ---------------------------------------------------------------------------


class ImmigrationCog(commands.Cog):
    """Delta City immigration: arrival station + interactive registration."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bot.add_listener(self._on_member_join, "on_member_join")
        bot.add_listener(self._on_member_leave, "on_member_remove")

        # Shared persistent views (survive restarts via bot.add_view).
        self.check_in_view = CheckInView()
        self.pending_view = PendingNoticeView()
        self.application_view = ApplicationView()
        self.state_view = StateSelectView()
        self._register_persistent_views()
        self._register_shorthand_commands()
    # -- helpers ------------------------------------------------------------

    @property
    def db(self):
        return self.bot.db

    def _arrival_channel(self, guild: discord.Guild):
        for ch in guild.text_channels:
            if permissions.is_arrival_channel(ch.name):
                return ch
        return None

    @staticmethod
    def _channel_for_member(guild: discord.Guild, member: discord.Member):
        """Prefer the arrival station, else any visible text channel."""
        channel = None
        for ch in guild.text_channels:
            if permissions.is_arrival_channel(ch.name):
                channel = ch
                break
        if channel is None:
            channel = guild.system_channel
        if channel is None:
            for ch in guild.text_channels:
                if ch.permissions_for(member).view_channel:
                    channel = ch
                    break
        return channel

    async def _start_session(self, member: discord.Member) -> None:
        """Create/refresh a registration session and present step 1."""
        if self.db is None:
            log.warning("No database available; registration unavailable.")
            return
        existing = self.db.get_citizen(member.id)
        if existing is not None:
            await member.send(render_already_registered(existing))
            return
        if self.db.has_active_registration(member.id):
            return  # already in progress; form already exists
        channel = self._channel_for_member(member.guild, member)
        if channel is None:
            # No visible channel to post the form in — do NOT create a
            # session row, or the user would be locked out of !arrival
            # with no form (and no cancel button) to act on.
            try:
                await member.send(
                    "🛬 WELCOME TO DELTA CITY\n\n"
                    "You have arrived at the Delta City border. "
                    "I couldn't post my registration form in the server — "
                    "an administrator should see you.",
                )
            except discord.Forbidden:
                log.warning(
                    "Could not DM %s (%s) — no visible channel.", member, member.id
                )
            return
        expires = (datetime.now(timezone.utc) + timedelta(seconds=REGISTRATION_TIMEOUT)).isoformat()
        self.db.create_registration_session(
            member.id, member.guild.id, expires,
            stage="city",
            channel_id=channel.id,
        )
        view = ImmigrationView(member.id, self)
        try:
            sent = await channel.send(content=render_arrival_header(member), view=view)
        except discord.HTTPException as exc:
            # The form never reached the channel — drop the session row so
            # the next !arrival starts clean instead of blocking on a ghost
            # session the user can neither see nor cancel.
            self.db.delete_registration_session(member.id)
            log.warning(
                "Could not post registration form for %s (%s) in %s: %s",
                member, member.id, channel, exc,
            )
            try:
                await member.send(
                    "🛬 WELCOME TO DELTA CITY\n\n"
                    "Something went wrong posting my registration form "
                    "in the server. Try `!arrival` again in a moment, or "
                    "ping an administrator."
                )
            except discord.Forbidden:
                pass
            return
        self.db.update_registration_session(
            member.id, message_id=sent.id, channel_id=channel.id
        )

    # -- events -------------------------------------------------------------

    async def _on_member_join(self, member: discord.Member) -> None:
        """Announce the arrival and begin registration in #arrival-station."""
        log.info("Member joined: %s (%s)", member, member.id)
        if member.bot:
            return
        await self._stamp_asylum_role(member)
        await self._announce_airport_arrival(member)
        if self.db is None:
            return
        existing = self.db.get_citizen(member.id)
        if existing is not None:
            # Returning citizen — welcome them back, no second identity.
            try:
                await member.send(
                    "🇩🇨 Welcome back, **%s**.\n\nCitizen ID: `%s`"
                    % (existing["name"], existing["citizen_id"])
                )
            except discord.Forbidden:
                pass
            return
        # Stamp Unverified — locks the newcomer to #airport and #citizens
        # until an officer completes their registration.
        try:
            await permissions.add_unverified_role(member)
        except discord.HTTPException as exc:
            log.warning("Could not stamp Unverified on %s: %s", member, exc)
        channel = self._arrival_channel(member.guild)
        if channel is not None:
            embed = discord.Embed(
                title="🛬 NEW ARRIVAL",
                description=(
                    "A new arrival has entered Delta City.\n\n"
                    "Citizen registration is required before entry into "
                    "the city can be granted."
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Arrival: {member.display_name}")
            await channel.send(
                f"{member.mention} — a new arrival has entered Delta City.",
                embed=embed,
            )
        await self._start_session(member)

    # -- airport flight announcements ---------------------------------------

    def _airport_channel(self, guild):
        """Delta International Airport channel from config, if present."""
        if guild is None or not settings.AIRPORT_CHANNEL_ID:
            return None
        return guild.get_channel(int(settings.AIRPORT_CHANNEL_ID))

    async def _stamp_asylum_role(self, member: discord.Member) -> None:
        """Stamp the Asylum role (idempotent); never blocks the join flow."""
        role = discord.utils.get(
            member.guild.roles, id=int(settings.ASYLUM_ROLE_ID)
        )
        if role is None:
            log.warning(
                "Asylum role %s not found in guild; not stamped.",
                settings.ASYLUM_ROLE_ID,
            )
            return
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="New arrival to Delta City (Asylum)")
            except discord.HTTPException as exc:
                log.warning("Could not stamp Asylum on %s: %s", member, exc)

    async def _announce_airport_arrival(self, member: discord.Member) -> None:
        """Post the flight-arrival announcement in the airport channel."""
        channel = self._airport_channel(member.guild)
        if channel is None:
            log.warning(
                "Airport channel %s not found; skipping arrival announcement.",
                settings.AIRPORT_CHANNEL_ID,
            )
            return
        message = build_arrival_message(
            member.mention,
            _dt.datetime.now().year,
            settings.RULES_CHANNEL_ID,
            settings.CITIZENSHIP_CHANNEL_ID,
            settings.IMMIGRATION_OFFICER_ROLE_ID,
        )
        allowed = discord.AllowedMentions(
            everyone=False,
            roles=[int(settings.IMMIGRATION_OFFICER_ROLE_ID)],
            users=[member.id],
        )
        try:
            await channel.send(message, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            log.warning(
                "Could not post arrival announcement in the airport channel: %s",
                exc,
            )

    async def _on_member_leave(self, member) -> None:
        """Post the flight-departure announcement in the airport channel."""
        log.info("Member left: %s (%s)", member, member.id)
        if getattr(member, "bot", False):
            return
        channel = self._airport_channel(getattr(member, "guild", None))
        if channel is None:
            log.warning(
                "Airport channel %s not found; skipping departure announcement.",
                settings.AIRPORT_CHANNEL_ID,
            )
            return
        name = getattr(member, "display_name", None) or str(member)
        message = build_departure_message(name)
        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=False
                ),
            )
        except discord.HTTPException as exc:
            log.warning(
                "Could not post departure announcement in the airport channel: %s",
                exc,
            )

    # -- commands -----------------------------------------------------------

    @commands.command(name="arrival", hidden=False)
    async def cmd_arrival(self, ctx: commands.Context) -> None:
        """Open the arrival/registration experience (or show progress)."""
        member = ctx.author
        if not (isinstance(member, discord.Member) and member.guild):
            await ctx.reply("🛬 Registration only works inside a server.")
            return
        if self.db is None:
            await ctx.reply("⚠️ Registration is currently unavailable.")
            return
        existing = self.db.get_citizen(member.id)
        if existing is not None:
            await ctx.reply(render_already_registered(existing))
            return
        session = self.db.get_registration_session(member.id)
        if session is not None and await self._form_message_alive(member.guild, session):
            await ctx.reply(
                "🛬 You already have a registration in progress. "
                "Find my form above — or run `!cancel_registration` "
                "to start over."
            )
            return
        if session is not None:
            # Stale session: the saved form message is gone (deleted, or the
            # post failed after the row was written). Drop it so the user is
            # not locked out with no form to interact with or cancel.
            self.db.delete_registration_session(member.id)
        await self._start_session(member)

    async def _form_message_alive(
        self, guild: discord.Guild, session: Mapping[str, Any]
    ) -> bool:
        """True if the form message stored in *session* is still fetchable."""
        message_id = session.get("message_id")
        channel_id = session.get("channel_id")
        if not message_id or not channel_id:
            return False
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.HTTPException:
            return False
        return message is not None

    @commands.command(name="cancel_registration", aliases=["cancelreg"], hidden=False)
    async def cmd_cancel_registration(self, ctx: commands.Context) -> None:
        """Cancel an in-progress registration session."""
        member = ctx.author
        if not (isinstance(member, discord.Member) and member.guild):
            await ctx.reply("⚠️ Cancellation only works inside a server.", ephemeral=True)
            return
        if self.db is None:
            await ctx.reply("⚠️ Registration is currently unavailable.", ephemeral=True)
            return
        if self.db.delete_registration_session(member.id):
            await ctx.reply(
                "🗑️ Registration cancelled. Use `!arrival` whenever you're "
                "ready to start again.",
                ephemeral=True,
            )
        else:
            await ctx.reply(
                "You don't have a registration in progress. Use `!arrival` "
                "to start one.",
                ephemeral=True,
            )

    @commands.command(name="register", hidden=False)
    async def cmd_register(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        city: str | None = None,
    ) -> None:
        """Open a citizen registration for another member.

        Not self-service: only an Immigration Officer, a Discord
        Administrator or the Chief Administrator can run `!register`
        (members self-register with `!arrival` instead).  Protected
        targets (Discord Admins / the Chief Administrator) can only be
        registered by the Chief Administrator.  Optionally pass a city
        name to pre-select it; the member then finishes the remaining
        steps on the posted form.
        """
        author = ctx.author
        if not isinstance(author, discord.Member) or author.guild is None:
            await ctx.reply("⚠️ !register only works inside a server.", ephemeral=True)
            return
        if member is None:
            await ctx.reply(
                "🛬 Usage: `!register @user [city]` — registration is "
                "carried out by an Immigration Officer, an Administrator "
                "or the Chief Administrator. To register yourself, use "
                "`!arrival`.",
                ephemeral=True,
            )
            return
        if member.guild is None or member.guild.id != author.guild.id:
            await ctx.reply(
                f"⚠️ You can only register members of this server. Got {member.mention}.",
                ephemeral=True,
            )
            return

        ok, reason = permissions.can_register(author, member)
        if not ok:
            await ctx.reply(reason, ephemeral=True)
            return

        if self.db is None:
            await ctx.reply("⚠️ The citizen database is unavailable.", ephemeral=True)
            return

        existing = self.db.get_citizen(member.id)
        if existing is not None:
            await ctx.reply(render_already_registered(existing), ephemeral=True)
            return

        if self.db.has_active_registration(member.id):
            await ctx.reply(
                f"🛬 {member.mention} already has a registration in "
                "progress — they can finish the form I already posted.",
                ephemeral=True,
            )
            return

        stage = "city"
        session_city: str | None = None
        if city is not None:
            canonical = identity.validate_city(city)
            if canonical is None:
                await ctx.reply(
                    "⚠️ Unknown city. Valid cities: "
                    + ", ".join(sorted(identity.CITY_PREFIXES)),
                    ephemeral=True,
                )
                return
            stage = "community"
            session_city = canonical

        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=REGISTRATION_TIMEOUT)
        ).isoformat()
        self.db.create_registration_session(
            member.id,
            member.guild.id,
            expires,
            stage=stage,
            channel_id=(
                ctx.channel.id if isinstance(ctx.channel, discord.TextChannel) else None
            ),
        )
        if session_city is not None:
            self.db.update_registration_session(member.id, city=session_city)

        view = ImmigrationView(member.id, self)
        try:
            sent = await ctx.reply(
                f"🛬 {member.mention}, an Immigration Officer has opened your "
                "citizen registration. Complete the form below to enter "
                "Delta City.",
                view=view,
                mention_everyone=False,
            )
        except discord.HTTPException as exc:
            log.warning("Could not post registration form for %s: %s", member.id, exc)
            self.db.delete_registration_session(member.id)
            await ctx.reply(
                "⚠️ I couldn't post the registration form. Try again.",
                ephemeral=True,
            )
            return
        self.db.update_registration_session(member.id, message_id=sent.id)

    @commands.command(name="id", hidden=False)
    async def cmd_id(self, ctx: commands.Context) -> None:
        """Show the invoking member's Citizen ID and identity card."""
        if self.db is None:
            await ctx.reply("⚠️ The citizen database is unavailable.")
            return
        record = self.db.get_citizen(ctx.author.id)
        if record is None:
            await ctx.reply(
                "You are not registered yet. Type `!arrival` to begin.",
                ephemeral=True,
            )
            return
        card_path = record.get("id_card_path")
        card_png = None
        if card_path:
            try:
                with open(card_path, "rb") as fh:
                    card_png = fh.read()
            except OSError:
                card_png = None
        embed = discord.Embed(
            title="🪪 DELTA CITY CITIZEN RECORD",
            description=render_citizen_profile(record),
            color=discord.Color.green()
            if record.get("status", "ACTIVE").upper() == "ACTIVE"
            else discord.Color.orange(),
        )
        kwargs = {"embed": embed}
        if card_png:
            kwargs["file"] = discord.File(
                io.BytesIO(card_png), filename=f"{record['citizen_id']}.png"
            )
        await ctx.reply(**kwargs)

    @commands.command(name="immigrate", aliases=["immmigrate"])
    @commands.has_permissions(administrator=True)
    async def cmd_immmigrate(self, ctx: commands.Context) -> None:
        """Admin: repair missing roles/cards for all registered citizens."""
        if not isinstance(ctx.guild, discord.Guild):
            return
        fixed = 0
        if self.db:
            for row in self.db.get_active_citizens():
                member = ctx.guild.get_member(row["discord_user_id"])
                if member is None:
                    continue
                try:
                    fixed += await self.db_repair(ctx.guild, member, row)
                except discord.HTTPException:
                    pass
        await ctx.reply(f"✅ Immigration audit complete ({fixed} repaired).")

    async def db_repair(self, guild, member, row) -> int:
        """Re-assign citizen roles and re-render the ID card if missing."""
        changed = 0
        if self.db is None:
            return 0
        city = row.get("state") or row.get("city") or ""
        portrait_path = row.get("portrait_path")
        card_path = row.get("id_card_path")
        portrait_png = b""
        if portrait_path:
            try:
                with open(portrait_path, "rb") as fh:
                    portrait_png = fh.read()
            except OSError:
                portrait_png = b""
        if not portrait_png:
            try:
                portrait_png = portrait.generate_portrait(row["citizen_id"])
            except Exception:  # noqa: BLE001
                portrait_png = b""
        card_png = b""
        if card_path:
            try:
                with open(card_path, "rb") as fh:
                    card_png = fh.read()
            except OSError:
                card_png = b""
        if not card_png and portrait_png:
            try:
                card_png = id_card.render_id_card(row, portrait_png)
            except Exception:  # noqa: BLE001
                card_png = b""
        if portrait_png or card_png:
            paths = _store_citizen_files(
                self.db, row["citizen_id"],
                portrait_png if portrait_png else None,
                card_png if card_png else None,
            )
            if paths:
                self.db.attach_citizen_files(row["discord_user_id"], **paths)
                changed += 1
        added: list[str] = []
        national = self._find_role(guild, "Deltaian")
        city_role = self._find_role(guild, f"{city} Citizen") if city else None
        if national and national not in member.roles:
            await member.add_roles(national, reason="Delta City immigration repair")
            added.append(national.name)
        if city_role and city_role not in member.roles:
            await member.add_roles(city_role, reason="Delta City immigration repair")
            added.append(city_role.name)
        if added:
            changed += 1
        return changed

# -- check-in flow (self-service) --------------------------------------

    @commands.command(name="check-in", aliases=["checkin"])
    async def cmd_check_in(self, ctx: commands.Context) -> None:
        """Begin the self-service check-in registration (Asylum role)."""
        if self.db is None:
            await ctx.reply("⚠️ The citizen database is unavailable.",
                            ephemeral=True)
            return
        if not permissions.is_asylum(ctx.author):
            await ctx.reply(
                "⚠️ Check-in is for members holding the Asylum role.",
                ephemeral=True,
            )
            return
        citizen = self.db.get_citizen(ctx.author.id)
        if citizen is not None:
            await ctx.reply(
                f"✅ You are already a citizen ({citizen['citizen_id']}).",
                ephemeral=True,
            )
            return
        existing = self.db.get_pending_application(ctx.author.id)
        if existing is not None:
            await ctx.reply(PENDING_NOTICE, view=PendingNoticeView(),
                            ephemeral=True)
            return
        await ctx.reply(
            "🛆 Welcome to Delta City port. Begin your registration below.",
            view=CheckInView(),
        )

    @commands.command(name="role", hidden=True)
    async def cmd_role(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """Show the dropdown of roles the invoker may assign or remove."""
        if member is None:
            await ctx.reply(
                "🛂 Usage: `!role @user` — or use the shorthand "
                "`!RoleName @user`.",
                ephemeral=True,
            )
            return
        if not permissions.assignable_role_names(ctx.author):
            await ctx.reply(
                "⚠️ You may not assign any Delta City roles.",
                ephemeral=True,
            )
            return
        await ctx.reply(
            f"Choose a role for {member.mention}.",
            view=RoleSelectView(self, ctx.author, member),
            ephemeral=True,
        )

    async def _on_begin_pressed(
        self, interaction: discord.Interaction
    ) -> None:
        """Begin button -> open the registration modal."""
        member = self._member_from(interaction.user)
        if member is None:
            await interaction.response.send_message(
                "⚠️ This works only inside a server.", ephemeral=True
            )
            return
        if self.db is None:
            await interaction.response.send_message(
                "⚠️ The citizen database is unavailable.", ephemeral=True
            )
            return
        if not permissions.is_asylum(member):
            await interaction.response.send_message(
                "⚠️ Check-in is for members holding the Asylum role.",
                ephemeral=True,
            )
            return
        citizen = self.db.get_citizen(member.id)
        if citizen is not None:
            await interaction.response.send_message(
                f"✅ You are already a citizen ({citizen['citizen_id']}).",
                ephemeral=True,
            )
            return
        if self.db.get_pending_application(member.id) is not None:
            await interaction.response.send_message(
                PENDING_NOTICE, view=PendingNoticeView(), ephemeral=True
            )
            return
        await interaction.response.send_modal(RegistrationModal())

    async def _on_cancel_restart_pressed(
        self, interaction: discord.Interaction
    ) -> None:
        """'Cancel and start over' -> clear the pending row, fresh form."""
        member = self._member_from(interaction.user)
        if member is None:
            return
        if self.db is not None:
            self.db.delete_application(member.id)
        try:
            await interaction.response.edit_message(
                "🛆 Start again when you are ready.", view=CheckInView()
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "🛆 Start again when you are ready.",
                view=CheckInView(),
                ephemeral=True,
            )

    async def _on_modal_submitted(
        self,
        interaction: discord.Interaction,
        character_name: str,
        date_of_birth: str,
        gender: str,
        city: str,
    ) -> None:
        """Modal step 1 -> validate, persist, then ask for the home state."""
        member = self._member_from(interaction.user)
        if member is None:
            await interaction.response.send_message(
                "⚠️ This works only inside a server.", ephemeral=True
            )
            return
        try:
            fields = sanitize_modal_fields(
                character_name, date_of_birth, gender, city
            )
        except RegistrationError as exc:
            await interaction.response.send_message(
                f"⚠️ {exc}", ephemeral=True
            )
            return
        if self.db is None:
            await interaction.response.send_message(
                "⚠️ The citizen database is unavailable.", ephemeral=True
            )
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        self.db.create_application(
            member.id,
            interaction.guild_id,
            fields["character_name"],
            fields["date_of_birth"],
            fields["gender"],
            fields["city"],
        )
        await interaction.followup.send(
            "🛂 Almost there — choose your home state.",
            view=StateSelectView(),
            ephemeral=True,
        )

    async def _on_state_selected(
        self, interaction: discord.Interaction
    ) -> None:
        """State select step 2 -> save the state and post the application."""
        member = self._member_from(interaction.user)
        if member is None:
            await interaction.response.defer(ephemeral=True)
            return
        if self.db is None:
            await interaction.response.send_message(
                "⚠️ The citizen database is unavailable.", ephemeral=True
            )
            return
        state = interaction.data["values"][0]
        pending = self.db.get_pending_application(member.id)
        if pending is None:
            await interaction.response.send_message(
                "⚠️ No application in progress — run `!check-in` to start.",
                ephemeral=True,
            )
            return
        self.db.update_application_state(member.id, state)
        pending = self.db.get_pending_application(member.id)
        guild = member.guild
        office = self._office_channel(guild)
        lines = [
            f"🛂 **New application** from {member.mention}",
            f"📛 Character name: {pending['character_name']}",
            f"🎂 Date of birth: {pending['date_of_birth'] or '—'}",
            f"⚧ Gender: {pending['gender']}",
            f"🏙️ City: {pending['city']}",
            f"🗺️ Home state: {state}",
        ]
        if office is not None:
            await office.send(
                "\n".join(lines), view=ApplicationView(self, member.id)
            )
        await interaction.response.send_message(
            "✅ Application submitted. An officer will review it in "
            "the immigration office.",
            ephemeral=True,
        )

    async def _on_approve_pressed(
        self, interaction: discord.Interaction
    ) -> None:
        """Approve -> issue the citizen, swap roles, post the certificate."""
        user_id = int(interaction.data["component_id"].rsplit(":", 1)[1])
        if not (
            permissions.is_immigration_officer(interaction.user)
            or permissions.is_chief_admin(interaction.user)
        ):
            await interaction.response.send_message(
                "⚠️ Only officers may approve applications.",
                ephemeral=True,
            )
            return
        if self.db is None:
            await interaction.response.send_message(
                "⚠️ The citizen database is unavailable.", ephemeral=True
            )
            return
        pending = self.db.get_pending_application(user_id)
        if pending is None:
            await interaction.response.edit_message(
                content="⚠️ No pending application for that member.",
                view=None,
            )
            return
        await interaction.response.defer()
        guild = interaction.guild
        state = pending["state"]
        if not state:
            await interaction.followup.send(
                "⚠️ The application has no home state recorded.",
                ephemeral=True,
            )
            await interaction.edit_original_response(
                content="⚠️ **Cannot approve** — no home state recorded.",
                view=None,
            )
            return
        try:
            record, created = self.db.register_citizen(
                user_id,
                name=pending["character_name"],
                gender=pending["gender"],
                state=state,
                date_of_birth=pending["date_of_birth"],
            )
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        if not created:
            await interaction.followup.send(
                f"✅ That member is already a citizen "
                f"({record['citizen_id']}).",
                ephemeral=True,
            )
            await interaction.edit_original_response(
                content="✅ **Approved** (member was already a citizen).",
                view=None,
            )
            return
        self.db.set_application_status(user_id, "approved")
        member = guild.get_member(user_id)
        if member is not None:
            await self._apply_role_change(
                interaction.user, member, settings.CITIZEN_ROLE_NAME
            )
            await self._apply_role_change(
                interaction.user, member, f"{state} Citizen"
            )
        certificate = record["citizen_id"]
        lines = [
            f"📜 **Birth certificate {certificate}**",
            f"📛 {record['name']}",
            f"🎂 {record['date_of_birth'] or '—'}",
            f"⚧ {record['gender']}",
            f"🏙️ {pending['city']}, {state}",
        ]
        cert_channel = self._certificate_channel(guild)
        if cert_channel is not None:
            await cert_channel.send("\n".join(lines))
        if member is not None:
            lines2 = [
                f"🛬 **{member.mention}** has become a Deltaian citizen: "
                f"**{record['name']}** ({certificate})."
            ]
            ee = self._entry_exit_channel(guild)
            if ee is not None:
                await ee.send("\n".join(lines2))
        await interaction.edit_original_response(
            content=f"✅ **Approved** — {certificate} issued.",
            view=None,
        )

    async def _on_reject_pressed(
        self, interaction: discord.Interaction
    ) -> None:
        """Reject -> notify the applicant and clear the pending row."""
        user_id = int(interaction.data["component_id"].rsplit(":", 1)[1])
        if not (
            permissions.is_immigration_officer(interaction.user)
            or permissions.is_chief_admin(interaction.user)
        ):
            await interaction.response.send_message(
                "⚠️ Only officers may reject applications.",
                ephemeral=True,
            )
            return
        if self.db is None:
            await interaction.response.send_message(
                "⚠️ The citizen database is unavailable.", ephemeral=True
            )
            return
        pending = self.db.get_pending_application(user_id)
        if pending is None:
            await interaction.response.edit_message(
                content="⚠️ No pending application for that member.",
                view=None,
            )
            return
        self.db.delete_application(user_id)
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        if member is not None:
            try:
                await member.send(
                    "⚠️ Your Delta City application was not approved. "
                    "You may run `!check-in` to try again."
                )
            except discord.HTTPException:
                pass
        await interaction.edit_original_response(
            content="❌ **Rejected**.",
            view=None,
        )

    async def _apply_role_change(
        self, invoker, target: discord.Member, role_name: str
    ) -> None:
        """Assign or remove a role on the target (idempotent toggle)."""
        role = None
        for r in target.guild.roles:
            if r.name == role_name:
                role = r
                break
        if role is None:
            log.warning(
                "Role %r not found in guild %s", role_name, target.guild.id
            )
            return
        try:
            if role in target.roles:
                await target.remove_roles(
                    role, reason=f"Removed by {invoker} (Delta City)"
                )
            else:
                await target.add_roles(
                    role, reason=f"Assigned by {invoker} (Delta City)"
                )
        except discord.Forbidden:
            log.warning("Missing permission to change role %s", role_name)    # -- setup helpers ---------------------------------------------------------

    def _register_persistent_views(self):
        add = getattr(self.bot, "add_view", None)
        if not callable(add):
            return
        for view in (
            self.check_in_view,
            self.pending_view,
            self.application_view,
            self.state_view,
        ):
            try:
                add(view)
            except Exception as exc:  # pragma: no cover - mock bots
                log.warning("Could not register persistent view: %s", exc)

    def _register_shorthand_commands(self):
        """Register !RoleName shorthand commands for every assignable role.

        Skips names that are taken by existing commands (e.g. 'citizen')
        and names that are not valid Discord command names.
        """
        add = getattr(self.bot, "add_command", None)
        if not callable(add):
            return
        for role_name in permissions.assignable_role_names(
            self._chief_actor_stub()
        ):
            cmd_name = command_name_for_role(role_name)
            if not re.fullmatch(r"[a-z0-9_]{1,32}", cmd_name):
                continue
            if getattr(self.bot, "get_command", None) and self.bot.get_command(
                cmd_name
            ):
                continue
            cmd = commands.command(
                name=cmd_name,
            )(
                self._make_shorthand_command(role_name)
            )
            try:
                add(cmd)
            except Exception as exc:  # pragma: no cover - duplicate guard
                log.warning("Could not register shorthand !%s: %s", cmd_name, exc)

    def _chief_actor_stub(self):
        """A stand-in actor that is treated as Chief Administrator so the
        full assignable-role list is returned for command registration."""

        class _Stub:
            @property
            def roles(self):
                return [type("_Role", (), {"name": settings.CHIEF_ADMIN_ROLE})()]

            @property
            def guild(self):
                class _G:
                    owner_id = None

                return _G()

            guild_permissions = None

        return _Stub()

    def _make_shorthand_command(self, role_name: str):
        async def _shorthand(ctx: commands.Context, *, member: Optional[discord.Member] = None):
            if member is None:
                await ctx.reply(
                    f"Usage: `!{role_name} @user` — assign or remove the "
                    f"{role_name} role.",
                    ephemeral=True,
                )
                return
            ok, reason = permissions.can_assign_role(ctx.author, member, role_name)
            if not ok:
                await ctx.reply(f"⚠️ {reason}", ephemeral=True)
                return
            await self._apply_role_change(ctx.author, member, role_name)

        return _shorthand


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImmigrationCog(bot))
    # Register a shared persistent instance. In-session, interactions are
    # routed by message_id to the per-user views attached at send time;
    # after a bot restart those in-memory registrations are gone, so
    # discord.py falls back to this message-less registration and routes
    # stale interactions here. Each callback re-binds user_id from the
    # interaction and re-reads the session from SQLite, so the shared
    # instance is safe.
    bot.add_view(ImmigrationView(0))