"""Tests for the permission tier matrix and role exclusivity rules.

Real permission helpers are exercised with lightweight stand-ins for
``discord.Member`` / ``discord.Guild``: the helpers only read
``member.roles``, ``member.guild_permissions`` and ``member.guild.owner_id``
and resolve roles through ``discord.utils.get(guild.roles, name=...)``, so
plain objects with a ``name`` attribute are sufficient and no Discord
connection is needed.  ``Mock(spec=discord.Member)`` is used (not a plain
object) because the role helpers gate on
``isinstance(member, discord.Member)`` — constructing a real
``discord.Member`` requires a live ``ConnectionState`` and is impractical
in unit tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from delta_city import permissions
import delta_city.registration as reg

DELTAIAN_ROLE = "🇩🇨 Deltaian"
ASABA_ROLE = permissions.city_role_name("Asaba")
WARRI_ROLE = permissions.city_role_name("Warri")
UNVERIFIED = permissions.settings.UNVERIFIED_ROLE


def _guild(role_names):
    return SimpleNamespace(roles=[SimpleNamespace(name=n) for n in role_names])


def _member(guild, role_names, *, admin=False, owner_id=None, user_id=1):
    member = Mock(spec=discord.Member)
    member.id = user_id
    member.roles = [r for r in getattr(guild, "roles", ()) if r.name in role_names]
    member.guild = SimpleNamespace(
        roles=getattr(guild, "roles", []),
        owner_id=owner_id
        if owner_id is not None
        else getattr(guild, "owner_id", None),
    )
    member.guild_permissions = SimpleNamespace(administrator=admin)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def _plain_user():
    """A discord.User (not a Member) — e.g. a DM interaction."""
    return Mock(spec=discord.User)


# ---------------------------------------------------------------------------
# !register tier matrix
# ---------------------------------------------------------------------------


class TestCanRegister:
    def test_chief_admin_registers_regular_target(self):
        actor = _member(_guild(["Chief Administrator"]), ["Chief Administrator"])
        target = _member(_guild([]), [])
        assert permissions.can_register(actor, target) == (True, "")

    def test_guild_owner_is_chief_admin_tier(self):
        guild = SimpleNamespace(owner_id=7)
        actor = _member(guild, [], owner_id=7, user_id=7)
        target = _member(_guild([]), [])
        assert permissions.can_register(actor, target) == (True, "")

    def test_chief_admin_registers_protected_target(self):
        """The only tier allowed to touch Admin / Chief Admin accounts."""
        actor = _member(_guild(["Chief Administrator"]), ["Chief Administrator"])
        target = _member(_guild([]), [], admin=True)
        assert permissions.can_register(actor, target) == (True, "")

    def test_discord_admin_registers_regular_target(self):
        actor = _member(_guild([]), [], admin=True)
        target = _member(_guild([]), [])
        assert permissions.can_register(actor, target) == (True, "")

    def test_discord_admin_blocked_from_protected_target(self):
        actor = _member(_guild([]), [], admin=True)
        target = _member(_guild(["Chief Administrator"]), ["Chief Administrator"])
        ok, reason = permissions.can_register(actor, target)
        assert ok is False
        assert "Chief Administrator" in reason

    def test_immigration_officer_registers_regular_target(self):
        actor = _member(_guild(["Immigration Officer"]), ["Immigration Officer"])
        target = _member(_guild([]), [])
        assert permissions.can_register(actor, target) == (True, "")

    def test_immigration_officer_blocked_from_protected_target(self):
        actor = _member(_guild(["Immigration Officer"]), ["Immigration Officer"])
        target = _member(_guild([]), [], admin=True)
        ok, reason = permissions.can_register(actor, target)
        assert ok is False
        assert "protected" in reason.lower()

    def test_regular_member_cannot_register(self):
        actor = _member(_guild(["Unverified"]), ["Unverified"])
        target = _member(_guild([]), [])
        ok, reason = permissions.can_register(actor, target)
        assert ok is False
        assert "!register" in reason

    def test_officer_role_name_is_case_sensitive(self):
        """Role names must match exactly — no fuzzy tier escalation."""
        actor = _member(_guild(["immigration officer"]), ["immigration officer"])
        target = _member(_guild([]), [])
        assert permissions.can_register(actor, target)[0] is False

    def test_non_member_actor_cannot_register(self):
        ok, reason = permissions.can_register(_plain_user(), _member(_guild([]), []))
        assert ok is False
        assert "server" in reason.lower()


# ---------------------------------------------------------------------------
# !appoint / !dismiss tier matrix
# ---------------------------------------------------------------------------


class TestCanAppoint:
    @pytest.mark.parametrize(
        "roles,admin",
        [
            (["Chief Administrator"], False),
            ([], False),  # guild owner (owner_id matched)
            ([], True),  # Discord Administrator
            (["President"], False),
            (["Vice President"], False),
            (["Chief of Staff"], False),
        ],
    )
    def test_allowed_tiers(self, roles, admin):
        if roles == [] and not admin:
            guild = SimpleNamespace(owner_id=3)
            actor = _member(guild, roles, owner_id=3, user_id=3)
        else:
            actor = _member(_guild(roles), roles, admin=admin)
        assert permissions.can_appoint(actor) == (True, "")

    @pytest.mark.parametrize(
        "roles,admin",
        [
            (["Immigration Officer"], False),
            (["Unverified"], False),
            (["Citizen of Asaba"], False),
            ([], False),  # regular member, not the owner
        ],
    )
    def test_blocked_tiers(self, roles, admin):
        actor = _member(_guild(roles), roles, admin=admin)
        ok, reason = permissions.can_appoint(actor)
        assert ok is False
        assert "President" in reason and "Chief of Staff" in reason

    def test_non_member_actor_cannot_appoint(self):
        ok, reason = permissions.can_appoint(_plain_user())
        assert ok is False
        assert "server" in reason.lower()

    def test_is_appointment_actor_mirrors_can_appoint(self):
        president = _member(_guild(["President"]), ["President"])
        nobody = _member(_guild([]), [])
        assert permissions.is_appointment_actor(president) is True
        assert permissions.is_appointment_actor(nobody) is False


# ---------------------------------------------------------------------------
# Role exclusivity: one state at a time + Unverified removal
# ---------------------------------------------------------------------------


class TestAssignCitizenRolesExclusivity:
    def _guild(self):
        return _guild(
            [
                DELTAIAN_ROLE,
                ASABA_ROLE,
                "Asaba",  # legacy bare role
                WARRI_ROLE,
                UNVERIFIED,
            ]
        )

    def test_switching_city_strips_old_citizenship_and_unverified(self):
        guild = self._guild()
        member = _member(
            guild, [ASABA_ROLE, "Asaba", UNVERIFIED]
        )

        asyncio_run(
            reg.ImmigrationView._assign_citizen_roles(reg.ImmigrationView(0), guild, member, "Warri")
        )

        added = [c.args[0].name for c in member.add_roles.await_args_list]
        removed = [c.args[0].name for c in member.remove_roles.await_args_list]
        assert added == [DELTAIAN_ROLE, WARRI_ROLE]
        assert set(removed) == {ASABA_ROLE, "Asaba", UNVERIFIED}
        # National role is granted before the new city role.
        assert added[0] == DELTAIAN_ROLE

    def test_registering_same_city_is_idempotent(self):
        guild = self._guild()
        member = _member(guild, [DELTAIAN_ROLE, WARRI_ROLE])

        asyncio_run(
            reg.ImmigrationView._assign_citizen_roles(reg.ImmigrationView(0), guild, member, "Warri")
        )

        assert member.add_roles.await_count == 0
        assert member.remove_roles.await_count == 0

    def test_without_unverified_role_no_extraneous_removal(self):
        guild = self._guild()
        member = _member(guild, [ASABA_ROLE])

        asyncio_run(
            reg.ImmigrationView._assign_citizen_roles(reg.ImmigrationView(0), guild, member, "Warri")
        )

        removed = [c.args[0].name for c in member.remove_roles.await_args_list]
        assert UNVERIFIED not in removed
        assert removed == [ASABA_ROLE]

    def test_legacy_bare_role_alone_counts_as_citizenship(self):
        """A pre-immigration citizen holding only the bare 'Asaba' role is
        still switched cleanly to the new state."""
        guild = self._guild()
        member = _member(guild, ["Asaba"])

        asyncio_run(
            reg.ImmigrationView._assign_citizen_roles(reg.ImmigrationView(0), guild, member, "Warri")
        )

        removed = [c.args[0].name for c in member.remove_roles.await_args_list]
        added = [c.args[0].name for c in member.add_roles.await_args_list]
        assert removed == ["Asaba"]
        assert added == [DELTAIAN_ROLE, WARRI_ROLE]


def asyncio_run(coro):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)