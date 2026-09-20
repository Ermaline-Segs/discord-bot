"""Tests for the Delta International Airport join/leave announcements.

The message builders are pure functions, so they are tested directly; the
cog event handlers are driven with ``asyncio.run`` using the same
lightweight stand-in pattern as test_registration.py (``Mock(spec=discord.Member)``
members, ``SimpleNamespace`` guilds, ``AsyncMock`` for the Discord calls).
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.ext import commands

from config import settings
import commands.airport as airport


# ---------------------------------------------------------------------------
# stand-ins (same pattern as test_registration.py)
# ---------------------------------------------------------------------------

AIRPORT_ID = "123456789012345678"
RULES_ID = "990000000000000001"
CITIZENSHIP_ID = "990000000000000002"
OFFICER_ROLE_ID = "990000000000000003"


@pytest.fixture
def airport_settings(monkeypatch):
    """Pin the airport config values to known, stable test IDs."""
    monkeypatch.setattr(settings, "AIRPORT_CHANNEL_ID", AIRPORT_ID)
    monkeypatch.setattr(settings, "RULES_CHANNEL_ID", RULES_ID)
    monkeypatch.setattr(settings, "CITIZENSHIP_CHANNEL_ID", CITIZENSHIP_ID)
    monkeypatch.setattr(settings, "IMMIGRATION_OFFICER_ROLE_ID", OFFICER_ROLE_ID)


def _guild(channel=None, *, has_asylum_role=True, guild_id=777):
    """A fake guild whose ``get_channel`` only knows the airport channel."""
    roles = [SimpleNamespace(name="Asylum")] if has_asylum_role else []
    return SimpleNamespace(
        id=guild_id,
        name="Delta City",
        roles=roles,
        get_channel=lambda cid: channel
        if str(cid) == str(AIRPORT_ID)
        else None,
    )


def _member(*, bot=False, channel=None, has_asylum_role=True, name="New Resident"):
    """A fake member with an AsyncMock add_roles, guild, and channel."""
    member = Mock(spec=discord.Member)
    member.id = 424242
    member.bot = bot
    member.mention = f"<@{member.id}>"
    member.display_name = name
    member.roles = []
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.guild = _guild(channel=channel, has_asylum_role=has_asylum_role)
    return member


def _cog():
    return airport.Airport(Mock(spec=commands.Bot))


# ---------------------------------------------------------------------------
# arrival message builder
# ---------------------------------------------------------------------------


class TestArrivalMessage:
    def test_placeholder_substitution(self, airport_settings):
        member = _member()
        text = airport.build_arrival_message(member)
        assert f"<@{member.id}>" in text
        assert f"<#{RULES_ID}>" in text
        assert f"<#{CITIZENSHIP_ID}>" in text
        assert f"<@&{OFFICER_ROLE_ID}>" in text
        # nothing else may be mentioned
        assert text.count("<@") == 2  # the member and the officer role

    def test_uses_current_year_by_default(self, airport_settings):
        text = airport.build_arrival_message(_member())
        assert f"Flight **DC-{date.today().year}**" in text

    def test_explicit_year_is_respected(self, airport_settings):
        text = airport.build_arrival_message(_member(), year=2031)
        assert "Flight **DC-2031**" in text

    def test_contains_checklist_and_status(self, airport_settings):
        text = airport.build_arrival_message(_member())
        assert "**1.** Read the city regulations in" in text
        assert "**2.** Apply for citizenship in" in text
        assert "`!check-in`" in text
        assert "**3.** Wait at arrivals for an immigration officer" in text
        assert "🛂 **Status:** Asylum, awaiting clearance" in text


# ---------------------------------------------------------------------------
# departure message builder
# ---------------------------------------------------------------------------


class TestDepartureMessage:
    def test_name_appears_in_message(self, airport_settings):
        text = airport.build_departure_message("Zoe Adeyemi")
        assert "Zoe Adeyemi has boarded an outbound flight" in text

    def test_markdown_in_name_is_escaped(self, airport_settings):
        text = airport.build_departure_message("Big *Star*_Player_")
        assert "*Star*" not in text
        assert "_Player_" not in text
        assert r"\*Star\*" in text and r"\_Player\_" in text

    def test_mentions_in_name_are_neutralised(self, airport_settings):
        # escape_mentions only touches 17-20 digit user mentions.
        name = "<@12345678901234567> Owner"
        text = airport.build_departure_message(name)
        assert name not in text
        # a zero-width space is inserted right after the @, killing the ping
        assert "<@\u200b12345678901234567>" in text


# ---------------------------------------------------------------------------
# on_member_join
# ---------------------------------------------------------------------------


class TestOnMemberJoin:
    def test_posts_arrival_with_allowed_mentions(self, airport_settings, caplog):
        channel = Mock()
        channel.send = AsyncMock()
        cog = _cog()
        asyncio.run(cog._on_member_join(_member(channel=channel)))

        assert channel.send.await_count == 1
        text, kwargs = channel.send.call_args.args[0], channel.send.call_args.kwargs
        assert f"<@424242>" in text
        allowed = kwargs["allowed_mentions"]
        assert allowed.users == [424242]
        assert allowed.roles == [int(OFFICER_ROLE_ID)]
        assert allowed.everyone is False

    def test_bots_are_ignored(self, airport_settings):
        channel = Mock()
        channel.send = AsyncMock()
        cog = _cog()
        bot_member = _member(bot=True, channel=channel)
        asyncio.run(cog._on_member_join(bot_member))

        channel.send.assert_not_awaited()
        bot_member.add_roles.assert_not_awaited()

    def test_grants_asylum_role_when_missing(self, airport_settings):
        channel = Mock()
        channel.send = AsyncMock()
        cog = _cog()
        member = _member(channel=channel)
        asyncio.run(cog._on_member_join(member))

        (role,), kwargs = member.add_roles.call_args.args, member.add_roles.call_args.kwargs
        assert role.name == "Asylum"
        assert "reason" in kwargs

    def test_does_not_readd_asylum_role_when_present(self, airport_settings):
        channel = Mock()
        channel.send = AsyncMock()
        cog = _cog()
        member = _member(channel=channel)
        member.roles = [SimpleNamespace(name="Asylum")]
        asyncio.run(cog._on_member_join(member))

        member.add_roles.assert_not_awaited()
        channel.send.assert_awaited_once()

    def test_missing_channel_logs_warning_but_still_grants_role(
        self, airport_settings, caplog
    ):
        cog = _cog()
        member = _member(channel=None)
        with caplog.at_level("WARNING", logger="commands.airport"):
            asyncio.run(cog._on_member_join(member))

        member.add_roles.assert_awaited_once()
        assert any("not found" in rec.message for rec in caplog.records)

    def test_unset_channel_id_skips_posting(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "AIRPORT_CHANNEL_ID", "")
        cog = _cog()
        member = _member(channel=None)
        with caplog.at_level("WARNING", logger="commands.airport"):
            asyncio.run(cog._on_member_join(member))

        member.add_roles.assert_awaited_once()
        assert any("not set" in rec.message for rec in caplog.records)

    def test_send_failure_does_not_block_role_grant(self, airport_settings):
        channel = Mock()
        channel.id = int(AIRPORT_ID)
        channel.send = AsyncMock(side_effect=discord.Forbidden(Mock(), "no send"))
        cog = _cog()
        member = _member(channel=channel)

        asyncio.run(cog._on_member_join(member))  # must not raise

        member.add_roles.assert_awaited_once()
        channel.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# on_member_remove
# ---------------------------------------------------------------------------


class TestOnMemberRemove:
    def test_posts_departure_with_no_mentions_allowed(self, airport_settings):
        channel = Mock()
        channel.send = AsyncMock()
        cog = _cog()
        member = _member(channel=channel, name="Zoe Adeyemi")
        asyncio.run(cog._on_member_remove(member))

        assert channel.send.await_count == 1
        text, kwargs = channel.send.call_args.args[0], channel.send.call_args.kwargs
        assert "Zoe Adeyemi has boarded an outbound flight" in text
        assert "<@" not in text
        allowed = kwargs["allowed_mentions"]
        assert allowed.users == []
        assert allowed.roles == []
        assert allowed.everyone is False

    def test_bots_are_ignored_on_leave(self, airport_settings):
        channel = Mock()
        channel.send = AsyncMock()
        cog = _cog()
        asyncio.run(cog._on_member_remove(_member(bot=True, channel=channel)))

        channel.send.assert_not_awaited()

    def test_missing_channel_does_not_raise(self, airport_settings, caplog):
        cog = _cog()
        member = _member(channel=None)
        with caplog.at_level("WARNING", logger="commands.airport"):
            asyncio.run(cog._on_member_remove(member))  # must not raise

        assert any("not found" in rec.message for rec in caplog.records)

    def test_send_failure_does_not_raise(self, airport_settings):
        channel = Mock()
        channel.id = int(AIRPORT_ID)
        channel.send = AsyncMock(side_effect=discord.Forbidden(Mock(), "no send"))
        cog = _cog()
        asyncio.run(cog._on_member_remove(_member(channel=channel)))  # must not raise
        channel.send.assert_awaited_once()