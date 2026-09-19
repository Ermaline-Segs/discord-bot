"""Discord role and channel-permission helpers for DELTA CITY.

Two layers:

Pure logic (unit-testable, no Discord connection)
-------------------------------------------------
- the official role names used by the immigration system
  (``🇩🇨 Deltaian`` national role, ``🏙️ Asaba Citizen`` ... city roles);
- channel-name matching so the admin setup can find the #arrival-station
  channel, the national channels and each city's channels no matter what
  exact names the server owner used;
- the permission overrides computed for each discovered channel, as plain
  dicts a guild can apply.

Async Discord glue
------------------
- :func:`setup_arrival_permissions` — finds or creates #arrival-station and
  locks it down to everyone (it is the only city channel unregistered
  members may see);
- :func:`setup_city_permissions` — for every city, denies @everyone view
  access to that city's channels and grants it to the city's citizen
  roles (both the official emoji role and the legacy bare state role, so
  citizens registered through the older ``!register`` flow keep working);
- :func:`setup_national_permissions` — locks the national channels to the
  ``🇩🇨 Deltaian`` role;
- :func:`setup_all_permissions` — the admin one-shot that runs all three
  and returns a human-readable report.

These functions are idempotent: running them twice leaves the server in
the same state.  They only ever *add* the overrides described above and
never delete role mentions on channels they do not own, so existing
server configuration is preserved.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

import discord
from discord import Guild, Permissions

from config import settings
from delta_city import identity

log = logging.getLogger("deltacity.permissions")

# ---------------------------------------------------------------------------
# Official role names
# ---------------------------------------------------------------------------

#: The national role every registered citizen receives. Granting this
#: role the view permission on the national category is what separates
#: "national access" from "city access".
DELTAIAN_ROLE_NAME = "🇩🇨 Deltaian"

#: Emoji used in front of each city's citizen role, per the immigration
#: spec.
CITY_ROLE_EMOJI = {
    "Asaba": "🏙️",
    "Warri": "🌆",
    "Ughelli": "🌆",
    "Ozoro": "🏘️",
    "Kwale": "🏘️",
    "Agbor": "🏙️",
}

#: The #arrival-station channel name (lowercase, as Discord stores it).
ARRIVAL_CHANNEL_NAME = "arrival-station"

#: Channel-name prefixes that mark a channel as belonging to the national
#: (rather than a single city's) area.
NATIONAL_CHANNEL_MARKERS = ("national", "immigration", "nation-")


# ---------------------------------------------------------------------------
# Role names
# ---------------------------------------------------------------------------


def city_role_name(city: str) -> str:
    """Official citizen role name for *city*, e.g. ``"🏙️ Asaba Citizen"``.

    Raises ``KeyError`` for unknown cities — callers must validate the
    city with :func:`delta_city.identity.validate_city` first.
    """
    if city not in identity.CITY_PREFIXES:
        raise KeyError(f"Unknown city: {city}")
    return f"{CITY_ROLE_EMOJI[city]} {city} Citizen"


def city_role_names(city: str) -> list[str]:
    """Every role name that counts as *city* citizen access.

    Returns the official emoji role first, followed by the legacy bare
    state role (``"Asaba"``) used by the pre-immigration ``!register``
    flow.  Granting both keeps old citizens' channel access intact.
    """
    if city not in identity.CITY_PREFIXES:
        raise KeyError(f"Unknown city: {city}")
    return [city_role_name(city), city]


def find_city_role(guild: Guild, city: str) -> discord.Role | None:
    """Return the first citizen role for *city* that exists in *guild*."""
    for name in city_role_names(city):
        role = discord.utils.get(guild.roles, name=name)
        if role is not None:
            return role
    return None


# ---------------------------------------------------------------------------
# Permission tiers (pure logic — testable without a guild)
# ---------------------------------------------------------------------------


def has_role(member, role_name: str) -> bool:
    """True if *member* holds a role named exactly *role_name*."""
    if not isinstance(member, discord.Member):
        return False
    return any(r.name == role_name for r in member.roles)


def is_discord_admin(member) -> bool:
    """True for Discord server Administrators."""
    return isinstance(member, discord.Member) and bool(
        getattr(member, "guild_permissions", None)
        and member.guild_permissions.administrator
    )


def is_chief_admin(member) -> bool:
    """Chief Administrator role, or the guild owner (full override tier)."""
    if not isinstance(member, discord.Member):
        return False
    guild = getattr(member, "guild", None)
    if guild is not None and guild.owner_id == member.id:
        return True
    return has_role(member, settings.CHIEF_ADMIN_ROLE)


def is_immigration_officer(member) -> bool:
    return has_role(member, settings.IMMIGRATION_OFFICER_ROLE)


def is_unverified(member) -> bool:
    return has_role(member, settings.UNVERIFIED_ROLE)


def is_protected_target(member) -> bool:
    """Admin / Chief Administrator accounts — only the Chief Admin may
    register or modify them."""
    return is_chief_admin(member) or is_discord_admin(member)


def can_register(actor, target) -> tuple[bool, str]:
    """May *actor* run !register against *target*?

    Allowed: Chief Administrator (always), Discord Administrators and
    Immigration Officers (for non-protected targets).  Protected targets
    (Admin or Chief Admin) are locked to the Chief Administrator only.
    """
    if not isinstance(actor, discord.Member):
        return False, "You can only run !register from inside a server."
    if is_protected_target(target):
        if is_chief_admin(actor):
            return True, ""
        return False, (
            "🛑 **{name}** is a protected account (Admin / Chief Administrator). "
            "Only the **Chief Administrator** can register or modify them."
        ).format(name=target)
    if is_chief_admin(actor) or is_discord_admin(actor) or is_immigration_officer(actor):
        return True, ""
    return False, (
        "Only a server **Administrator**, an **Immigration Officer**, or the "
        "**Chief Administrator** can run !register."
    )


def can_appoint(actor) -> tuple[bool, str]:
    """May *actor* run !appoint / !dismiss?

    Allowed: Chief Administrator, Discord Administrators, and holders of
    any role in ``settings.APPOINT_ALLOWED_ROLES`` (President, Vice
    President, Chief of Staff).
    """
    if not isinstance(actor, discord.Member):
        return False, "You can only run that command from inside a server."
    if is_chief_admin(actor) or is_discord_admin(actor):
        return True, ""
    for role_name in settings.APPOINT_ALLOWED_ROLES:
        if has_role(actor, role_name):
            return True, ""
    return False, (
        "Only a server **Administrator**, the **Chief Administrator**, the "
        "**President**, the **Vice President**, or the **Chief of Staff** can "
        "run !appoint / !dismiss."
    )


def is_appointment_actor(actor) -> bool:
    """Boolean-only variant of :func:`can_appoint`."""
    return can_appoint(actor)[0]


# ---------------------------------------------------------------------------
# Channel-name matching (pure logic)
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    return (name or "").strip().lower()


def is_arrival_channel(name: str) -> bool:
    """True if *name* is the #airport channel (legacy: #arrival-station)."""
    return _normalise(name) in {
        ARRIVAL_CHANNEL_NAME,
        settings.AIRPORT_CHANNEL_NAME,
    }


def is_airport_channel(name: str) -> bool:
    """True for the #airport channel where new arrivals check in."""
    return is_arrival_channel(name)


def is_citizens_channel(name: str) -> bool:
    """True for the #citizens channel where members post their interest."""
    return _normalise(name) == settings.CITIZENS_CHANNEL_NAME


def is_public_channel(name: str) -> bool:
    """The ONLY channels visible to Unverified members: #airport + #citizens."""
    return is_airport_channel(name) or is_citizens_channel(name)


def city_of_channel(name: str) -> str | None:
    """Return the city a channel belongs to, based on its name.

    Matches the city name itself ("asaba") or a ``<city>-<something>``
    prefix ("asaba-general", "asaba_news").  A national channel never
    matches a city.  Returns ``None`` for channels that are neither a
    city channel nor a national channel (rules, info, general chat, ...).
    """
    name = _normalise(name)
    if not name:
        return None
    for city in identity.CITY_PREFIXES:
        city_l = city.lower()
        if name == city_l or name.startswith(city_l + "-") or name.startswith(city_l + "_"):
            return city
    return None


def is_national_channel(name: str) -> bool:
    """True if *name* looks like a national (cross-city) channel.

    A channel is national when it carries a national marker
    (``national-announcements``, ``national-news``, ``immigration``) and
    does not belong to a single city.
    """
    name = _normalise(name)
    if city_of_channel(name) is not None:
        return False
    return any(marker in name for marker in NATIONAL_CHANNEL_MARKERS)


def is_public_info_channel(name: str) -> bool:
    """True for the channels that stay open to Unverified members.

    Per spec, Unverified members can only see #airport and #citizens —
    everything else is locked until registration removes the role.
    """
    return is_public_channel(name)


# ---------------------------------------------------------------------------
# Override computation (pure logic — testable without a guild)
# ---------------------------------------------------------------------------


def _deny_view() -> Permissions:
    p = Permissions.none()
    p.view_channel = False
    return p


def _allow_view() -> Permissions:
    p = Permissions.none()
    p.view_channel = True
    return p


def overrides_for_channel(
    channel_name: str,
    city_roles: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Permissions]:
    """Compute the permission overrides a channel should carry.

    *city_roles* maps city name -> list of role names (default: every
    role that grants access to that city, via :func:`city_role_names`).

    Returns ``{role_name: Permissions}``.  An empty dict means "leave
    this channel alone" (public info channels, category channels handled
    elsewhere, unknowns).
    """
    if city_roles is None:
        city_roles = {city: city_role_names(city) for city in identity.CITY_PREFIXES}

    name = _normalise(channel_name)
    overrides: dict[str, Permissions] = {
        "@everyone": _deny_view(),
    }

    if is_public_channel(name):
        # #airport and #citizens are the only channels visible to Unverified
        # members, so they stay open to everyone (view + send).
        open_p = Permissions.none()
        open_p.view_channel = True
        open_p.send_messages = True
        overrides["@everyone"] = open_p
        return overrides

    # Everything else is invisible to Unverified members.  The @everyone
    # deny above already blocks them on city/national channels; this deny
    # covers channels where @everyone would otherwise have view.
    overrides[settings.UNVERIFIED_ROLE] = _deny_view()

    city = city_of_channel(name)
    if city is not None:
        for role_name in city_roles.get(city, ()):
            overrides[role_name] = _allow_view()
        return overrides

    if is_national_channel(name):
        overrides[DELTAIAN_ROLE_NAME] = _allow_view()
        return overrides

    # Public info / unknown channels: untouched.
    return {}


def overrides_for_category(name: str) -> dict[str, Permissions]:
    """Permission overrides for a category, keyed on the category name.

    A category named after a city (``ASABA``, ``🏙️ Asaba``) or marked
    national (``🇩🇨 NATIONAL``) locks down every channel beneath it.
    """
    text = (name or "").strip()
    lowered = text.lower()

    if "national" in lowered or "🇩🇨" in text:
        return {
            "@everyone": _deny_view(),
            settings.UNVERIFIED_ROLE: _deny_view(),
            DELTAIAN_ROLE_NAME: _allow_view(),
        }

    for city in identity.CITY_PREFIXES:
        city_l = city.lower()
        if city_l in lowered:
            return {
                "@everyone": _deny_view(),
                settings.UNVERIFIED_ROLE: _deny_view(),
                **{role: _allow_view() for role in city_role_names(city)},
            }
    return {}


# ---------------------------------------------------------------------------
# Async Discord glue
# ---------------------------------------------------------------------------


def _role_by_name(guild: Guild, role_name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=role_name)


async def _ensure_role(guild: Guild, role_name: str, *, reason: str) -> discord.Role:
    """Find *role_name* in *guild*, creating it when missing."""
    existing = _role_by_name(guild, role_name)
    if existing is not None:
        return existing
    return await guild.create_role(name=role_name, reason=reason, hoist=False)
async def ensure_unverified_role(
    guild: Guild, *, reason: str = "Delta City unverified setup"
) -> discord.Role:
    """Find (or create) the Unverified role in *guild*."""
    return await _ensure_role(guild, settings.UNVERIFIED_ROLE, reason=reason)


async def add_unverified_role(
    member, *, reason: str = "New arrival to Delta City"
) -> discord.Role | None:
    """Stamp *member* with the Unverified role (idempotent)."""
    if not isinstance(member, discord.Member):
        return None
    role = _role_by_name(member.guild, settings.UNVERIFIED_ROLE)
    if role is None:
        return None
    if role not in member.roles:
        await member.add_roles(role, reason=reason)
    return role


async def remove_unverified_role(
    member, *, reason: str = "Registration complete"
) -> discord.Role | None:
    """Strip the Unverified role from *member* (idempotent)."""
    if not isinstance(member, discord.Member):
        return None
    role = _role_by_name(member.guild, settings.UNVERIFIED_ROLE)
    if role is None or role not in member.roles:
        return None
    await member.remove_roles(role, reason=reason)
    return role


async def setup_arrival_permissions(guild: Guild, *, reason: str = "Delta City arrival station setup") -> dict[str, Any]:
    """Find or create #arrival-station and open it to everyone.

    Returns a small report dict: ``{"channel": channel or None,
    "created": bool, "errors": [str, ...]}``.
    """
    report: dict[str, Any] = {"channel": None, "created": False, "errors": []}

    channel = discord.utils.get(guild.text_channels, name=ARRIVAL_CHANNEL_NAME)
    if channel is None:
        # A text channel at the bottom of the server.
        try:
            channel = await guild.create_text_channel(
                ARRIVAL_CHANNEL_NAME,
                reason=reason,
                topic="Entry point for all new arrivals to Delta City. "
                      "Complete citizen registration here.",
            )
            report["created"] = True
        except discord.Forbidden:
            report["errors"].append(
                "Missing Manage Channels permission — create a text channel "
                "named `arrival-station` manually."
            )
            return report

    report["channel"] = channel
    try:
        await channel.set_permissions(
            guild.default_role,
            reason=reason,
            view_channel=True,
            send_messages=True,
            add_reactions=False,
            attach_files=False,
        )
    except discord.Forbidden:
        report["errors"].append(
            "Could not edit arrival-station permissions (need Manage Channels)."
        )
    return report


async def setup_city_permissions(guild: Guild, *, reason: str = "Delta City city area lockdown") -> dict[str, Any]:
    """Lock every city's channels down to that city's citizen roles.

    Scans text channels (and voice channels) whose names match a city,
    plus categories named after a city, and applies the overrides
    computed by :func:`overrides_for_channel` /
    :func:`overrides_for_category`.  Channels that do not match any city
    are left untouched.
    """
    report: dict[str, Any] = {"locked": 0, "errors": []}

    for channel in list(guild.text_channels) + list(guild.voice_channels):
        overrides = overrides_for_channel(channel.name)
        if not overrides:
            continue
        await _apply_overrides(guild, channel, overrides, reason, report)

    for category in guild.categories:
        overrides = overrides_for_category(category.name)
        if not overrides:
            continue
        await _apply_overrides(guild, category, overrides, reason, report)

    return report


async def setup_national_permissions(guild: Guild, *, reason: str = "Delta City national area lockdown") -> dict[str, Any]:
    """Lock national channels to the 🇩🇨 Deltaian role (created if missing)."""
    report: dict[str, Any] = {"locked": 0, "errors": []}

    try:
        await _ensure_role(guild, DELTAIAN_ROLE_NAME, reason=reason)
    except discord.Forbidden:
        report["errors"].append(
            "Missing Manage Roles — create the `🇩🇨 Deltaian` role manually."
        )

    for channel in list(guild.text_channels) + list(guild.voice_channels):
        if not is_national_channel(channel.name):
            continue
        overrides = overrides_for_channel(channel.name)
        await _apply_overrides(guild, channel, overrides, reason, report)

    for category in guild.categories:
        overrides = overrides_for_category(category.name)
        if not overrides:
            continue
        if DELTAIAN_ROLE_NAME not in overrides and "@everyone" not in overrides:
            continue
        await _apply_overrides(guild, category, overrides, reason, report)

    return report


async def _apply_overrides(
    guild: Guild,
    target,
    overrides: dict[str, Permissions],
    reason: str,
    report: dict[str, Any],
) -> None:
    """Apply *overrides* to a channel or category, tolerating missing roles."""
    for role_name, permissions in overrides.items():
        if role_name == "@everyone":
            role: discord.Role | None = guild.default_role
        else:
            role = _role_by_name(guild, role_name)
            if role is None:
                # A role that does not exist yet (e.g. a city role before
                # any citizen registered) cannot be granted — skip it
                # silently; the role is created at registration time and
                # !setup-deltacity can be re-run to apply the override.
                continue
        try:
            await target.set_permissions(role, reason=reason, overwrite=permissions)
        except discord.Forbidden:
            report["errors"].append(
                f"Missing Manage Channels — could not lock `{target.name}` for {role_name}."
            )
            continue
        except discord.HTTPException as exc:
            report["errors"].append(f"`{target.name}`: {exc}")
            continue
        if role_name == "@everyone":
            report["locked"] += 1


async def setup_all_permissions(guild: Guild, *, reason: str | None = None) -> dict[str, Any]:
    """Run the full permission setup and return a combined report."""
    reason = reason or "Delta City permission setup"
    arrival = await setup_arrival_permissions(guild, reason=reason)
    cities = await setup_city_permissions(guild, reason=reason)
    national = await setup_national_permissions(guild, reason=reason)
    return {
        "arrival": arrival,
        "cities": cities,
        "national": national,
        "errors": arrival["errors"] + cities["errors"] + national["errors"],
    }


def format_setup_report(report: Mapping[str, Any]) -> str:
    """Render the combined report as a Discord message (2000-char safe)."""
    arrival = report.get("arrival", {})
    cities = report.get("cities", {})
    national = report.get("national", {})
    errors = report.get("errors", [])

    lines = ["✅ **DELTA CITY PERMISSIONS CONFIGURED**", ""]
    channel = arrival.get("channel")
    if channel is not None:
        suffix = " (created)" if arrival.get("created") else ""
64