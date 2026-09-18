"""Shared helpers for the DELTA CITY bot.

This module holds the small utilities that every command file needs:
- parsing what an admin typed for a government role
- deciding whether an office is unique or multi-holder
- creating/finding Discord roles safely (with hierarchy checks)
- formatting dates for Discord messages

No Discord I/O logic lives here except role lookup/creation, so it stays
easy to test.
"""

from __future__ import annotations

import discord
from config import settings


# ---------------------------------------------------------------------------
# Role name parsing
# ---------------------------------------------------------------------------

def state_role_name(title: str, state: str) -> str:
    """Full Discord role name for a state office, e.g. 'Governor of Asaba'."""
    return f"{title} of {state}"


def parse_role_input(role_input: str):
    """Turn what an admin typed into a real office.

    Accepts:
        "President"              -> national unique office
        "Senator"                -> national multi office
        "Governor Asaba"         -> state office (unique)
        "Commissioner Ughelli"   -> state office (multi)

    Returns a dict: {"title", "state", "role_name", "kind"}
        title     - exact office title, e.g. "Governor"
        state     - state name for state offices, None for national
        role_name - the Discord role name, e.g. "Governor of Asaba"
        kind      - "unique" (one holder) or "multi" (many holders)

    Returns None when the input does not name a valid office.
    """
    words = role_input.strip().split()
    if not words:
        return None

    state = None
    last = words[-1]
    matching_state = next(
        (s for s in settings.STATES if s.lower() == last.lower()), None
    )
    if matching_state:
        state = matching_state
        title_words = words[:-1]
    else:
        title_words = words

    title = " ".join(title_words)
    title_lower = title.lower()

    for role_list, kind in (
        (settings.NATIONAL_ROLES, "unique"),
        (settings.NATIONAL_MULTI_ROLES, "multi"),
    ):
        exact = next((r for r in role_list if r.lower() == title_lower), None)
        if exact is not None:
            return {
                "title": exact,
                "state": None,
                "role_name": exact,
                "kind": kind,
            }

    for role_list, kind in (
        (settings.STATE_ROLES, "unique"),
        (settings.STATE_MULTI_ROLES, "multi"),
    ):
        exact = next((r for r in role_list if r.lower() == title_lower), None)
        if exact is not None and state is not None:
            return {
                "title": exact,
                "state": state,
                "role_name": state_role_name(exact, state),
                "kind": kind,
            }
        if exact is not None and state is None:
            # A state office was given without a state - reject with detail.
            return {
                "title": exact,
                "state": None,
                "role_name": None,
                "kind": kind,
                "missing_state": True,
            }

    return None


# ---------------------------------------------------------------------------
# Discord role helpers
# ---------------------------------------------------------------------------

def find_role(guild: discord.Guild, role_name: str):
    """Return the guild role with this exact name, or None."""
    return discord.utils.get(guild.roles, name=role_name)


async def get_or_create_role(guild: discord.Guild, role_name: str):
    """Return the role by name, creating it at the bottom if missing.

    Returns (role, error). `error` is a user-facing string when the bot
    cannot create the role (e.g. missing Manage Roles permission).
    """
    role = find_role(guild, role_name)
    if role is not None:
        return role, None
    try:
        role = await guild.create_role(
            name=role_name,
            hoist=False,
            mentionable=False,
        )
    except discord.Forbidden:
        return None, (
            "I don't have permission to create roles. "
            "Please give me the **Manage Roles** permission."
        )
    return role, None


def hierarchy_error(guild: discord.Guild, role: discord.Role):
    """Check whether the bot can manage this role.

    Returns a user-facing error string when the bot's top role is BELOW the
    role it needs to assign, otherwise None.
    """
    me = guild.me
    if me is None:
        return None
    me_top = me.top_role
    # Roles at the very bottom are below everything; a bot managing only
    # bottom roles is fine.
    if role is guild.me.top_role or (role.position or 0) <= (me_top.position or 0):
        return None
    return (
        f"I can't assign the **{role.name}** role because it is higher than "
        f"my highest role (**{me_top.name}**). Please drag my role **above** "
        f"{role.name} in Server Settings -> Roles, then try again."
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def format_date(iso_string: str) -> str:
    """'2026-09-18T10:00:00+00:00' -> '18 September 2026' for messages."""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%d %B %Y")
    except (ValueError, TypeError):
        return iso_string


def mention(member: discord.Member) -> str:
    """Safe mention for a member (falls back to name for bots/unknowns)."""
    try:
        return member.mention
    except Exception:
        return f"@{member.display_name}"


def office_display_name(role_name: str) -> str:
    """'Governor of Asaba' -> 'Governor of Asaba' (kept for readability);
    national names pass through unchanged."""
    return role_name
