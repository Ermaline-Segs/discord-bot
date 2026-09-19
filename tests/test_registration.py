"""Tests for the Delta City registration session machinery (pure logic).

The Discord view itself is exercised indirectly here through the render
helpers and the session state machine; the cog wiring is covered by
import-time validation in the bot.  Command-level behaviour of the real
``!register`` callback is covered by :class:`TestRegisterCommand` below
using lightweight stand-ins (same pattern as test_permissions.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.ext import commands

import delta_city.registration as reg
from delta_city import identity, permissions


# ---------------------------------------------------------------------------
# stage machine
# ---------------------------------------------------------------------------

class TestStageMachine:
    def test_session_stage_defaults_to_city(self):
        assert reg.session_stage(None) == "city"
        assert reg.session_stage({}) == "city"

    def test_session_stage_rejects_unknown(self):
        assert reg.session_stage({"stage": "vibes"}) == "city"

    def test_stage_options_city_lists_all_cities(self):
        assert reg.stage_options("city") == list(identity.CITY_PREFIXES)

    def test_stage_options_community_requires_city(self):
        assert reg.stage_options("community") == []
        assert reg.stage_options("community", {"city": "Nowhere"}) == []

    def test_stage_options_community_matches_city(self):
        opts = reg.stage_options("community", {"city": "Warri"})
        assert opts == identity.CITY_COMMUNITIES["Warri"]
        assert "Pidgin" in opts and "Urhobo" in opts

    def test_stage_options_gender(self):
        assert reg.stage_options("gender") == identity.GENDER_OPTIONS

    def test_stage_options_review_empty(self):
        assert reg.stage_options("review") == []


class TestApplySelection:
    def _city_session(self, **kw):
        base = {"user_id": 1, "stage": "city", "expires_at": None}
        base.update(kw)
        return base

    def test_no_session_raises(self):
        with pytest.raises(ValueError):
            reg.apply_selection(None, "Asaba")

    def test_review_stage_raises(self):
        with pytest.raises(ValueError):
            reg.apply_selection(self._city_session(stage="review"), "Asaba")

    def test_invalid_city_raises(self):
        with pytest.raises(ValueError):
            reg.apply_selection(self._city_session(), "Lagos")

    def test_empty_choice_raises(self):
        with pytest.raises(ValueError):
            reg.apply_selection(self._city_session(), "   ")

    def test_city_selection_advances_and_resets_community(self):
        session = self._city_session(community="Anioma")
        updated, stage = reg.apply_selection(session, "Warri")
        assert stage == "community"
        assert updated["city"] == "Warri"
        assert updated["community"] is None  # new city invalidates old community

    def test_community_selection(self):
        session = self._city_session(stage="community", city="Warri")
        updated, stage = reg.apply_selection(session, "Itsekiri")
        assert stage == "gender"
        assert updated["community"] == "Itsekiri"

    def test_community_must_match_city(self):
        session = self._city_session(stage="community", city="Asaba")
        with pytest.raises(ValueError):
            reg.apply_selection(session, "Urhobo")

    def test_gender_selection_reaches_review(self):
        session = self._city_session(stage="gender", city="Warri",
                                     community="Itsekiri")
        updated, stage = reg.apply_selection(session, "Female")
        assert stage == "review"
        assert updated["gender"] == "Female"

    def test_original_session_not_mutated(self):
        session = self._city_session()
        reg.apply_selection(session, "Asaba")
        assert session.get("city") != "Asaba"
        assert session.get("stage") == "city"


class TestIsComplete:
    def test_not_complete_empty(self):
        assert reg.is_complete(None) is False
        assert reg.is_complete({}) is False

    def test_complete_requires_all_three(self):
        partial = {"city": "Warri", "community": "Itsekiri", "stage": "review"}
        assert reg.is_complete(partial) is False
        full = dict(partial, gender="Other")
        assert reg.is_complete(full) is True

    def test_complete_rejects_mismatched_community(self):
        bad = {"city": "Warri", "community": "Anioma", "gender": "Male",
               "stage": "review"}
        assert reg.is_complete(bad) is False


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_new_expiry_is_twelve_hours_ahead(self):
        now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
        stamp = reg.new_expiry(now=now)
        assert stamp == (now + timedelta(hours=12)).isoformat()

    def test_parse_expiry_invalid_returns_none(self):
        assert reg.parse_expiry(None) is None
        assert reg.parse_expiry("not-a-date") is None

    def test_parse_expiry_roundtrip(self):
        now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
        assert reg.parse_expiry(reg.new_expiry(now=now)) == now + timedelta(hours=12)

    def test_is_expired_semantics(self):
        now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
        live = reg.new_expiry(now=now)
        assert reg.is_expired(None, now=now) is True
        assert reg.is_expired({"expires_at": "garbage"}, now=now) is True
        assert reg.is_expired({"expires_at": live}, now=now) is False
        assert reg.is_expired({"expires_at": live},
                              now=now + timedelta(hours=13)) is True


# ---------------------------------------------------------------------------
# render helpers (pure strings)
# ---------------------------------------------------------------------------

class TestRenderHelpers:
    def test_welcome_border(self):
        assert "🛬 **WELCOME TO DELTA CITY**" in reg.WELCOME_BORDER
        assert "identity must be registered" in reg.WELCOME_BORDER

    def test_intro_fresh_session_shows_city_prompt(self):
        text = reg.render_intro()
        assert "Step 1 of 3" in text
        assert "Select your city." in text

    def test_intro_with_city_omits_step_label(self):
        text = reg.render_intro({"city": "Warri", "stage": "community"})
        assert "Step 1 of 3" not in text

    def test_step_content_confirms_choices(self):
        session = {"city": "Warri", "community": None, "gender": None,
                   "stage": "community"}
        text = reg.render_step_content(session)
        assert "✅ City selected: Warri" in text
        assert "Step 2 of 3" in text
        assert "✅ Community selected" not in text

    def test_review_content_shows_generated_name_and_disclaimer(self):
        session = {"city": "Asaba", "community": "Anioma", "gender": "Male",
                   "stage": "review"}
        text = reg.render_review_content(session, "Chukwudi Okafor")
        assert "Name: **Chukwudi Okafor**" in text
        assert "City: Asaba" in text
        assert "fictional" in text.lower()
        assert "Confirm" in text and "Regenerate" in text and "Cancel" in text

    def test_review_content_without_name(self):
        session = {"city": "Asaba", "community": "Anioma", "gender": "Male",
                   "stage": "review"}
        assert "Name: **—**" in reg.render_review_content(session, None)

    def test_processing_bar_frames(self):
        text = reg.render_processing(20, "Establishing citizenship record...")
        assert "⏳ **IDENTITY PROCESSING**" in text
        assert "▓▓░░░░░░░░ 20%" in text
        full = reg.render_processing(100, "Preparing identity card...")
        assert "▓▓▓▓▓▓▓▓▓▓ 100%" in full
        clamped = reg.render_processing(150, "x")
        assert "▓▓▓▓▓▓▓▓▓▓ 150%" in clamped  # bar clamped, label verbatim

    def test_processing_steps_cover_full_sequence(self):
        percents = [p for p, _ in reg.PROCESSING_STEPS]
        assert percents == [20, 40, 60, 80, 100]

    def test_complete_content(self):
        record = {
            "name": "Ada Ezenwa",
            "citizen_id": "DC-ASB-0001",
            "city": "Asaba",
            "community": "Anioma",
            "gender": "Female",
            "nationality": "Deltaian",
            "status": "ACTIVE",
        }
        text = reg.render_complete_content(record)
        assert "✅ **REGISTRATION COMPLETE**" in text
        assert "Name: Ada Ezenwa" in text
        assert "DC-ASB-0001" in text
        assert "Status: **ACTIVE**" in text

    def test_arrival_announcement(self):
        record = {
            "name": "Chukwudi Okafor",
            "citizen_id": "DC-ASB-0001",
            "city": "Asaba",
            "community": "Anioma",
        }
        text = reg.render_arrival_announcement(record)
        assert "🛬 **NEW CITIZEN ARRIVAL**" in text
        assert "NAME:\nChukwudi Okafor" in text
        assert "CITIZEN ID:\n`DC-ASB-0001`" in text
        assert "CITY:\nAsaba" in text
        assert "COMMUNITY:\nAnioma" in text
        assert "🟢 **ACTIVE**" in text

    def test_new_arrival_mentions_member(self):
        class FakeMember:
            mention = "<@12345>"
            def __str__(self):
                return "FakeMember"
        text = reg.render_new_arrival(FakeMember())
        assert "<@12345>" in text
        assert "🛬 **NEW ARRIVAL**" in text
        assert "registration is required" in text

    def test_already_registered(self):
        text = reg.render_already_registered({"citizen_id": "DC-WAR-0002"})
        assert "🇩🇨 You are already registered" in text
        assert "DC-WAR-0002" in text

    def test_session_expired_copy(self):
        text = reg.render_session_expired()
        assert "⌛ **SESSION EXPIRED**" in text
        assert "!arrival" in text

    def test_cancelled_copy(self):
        assert "❌ **REGISTRATION CANCELLED**" in reg.render_cancelled()

    def test_processing_failed_copy(self):
        text = reg.render_processing_failed("portrait API down")
        assert "⚠️ **PROCESSING PROBLEM**" in text
        assert "portrait API down" in text
        assert "Retry" in text
# ---------------------------------------------------------------------------
# !register command behaviour (lightweight stand-ins for ctx / member / db)
# ---------------------------------------------------------------------------


def _guild(role_names):
    return SimpleNamespace(
        id=99,
        roles=[SimpleNamespace(name=n) for n in role_names],
        owner_id=None,
        system_channel=None,
        text_channels=[],
    )


def _member(guild, role_names, *, admin=False, user_id=1):
    member = Mock(spec=discord.Member)
    member.id = user_id
    member.mention = f"<@{user_id}>"
    member.roles = [r for r in guild.roles if r.name in role_names]
    member.guild = guild
    member.guild_permissions = SimpleNamespace(administrator=admin)
    return member


def _db():
    return SimpleNamespace(
        get_citizen=Mock(return_value=None),
        has_active_registration=Mock(return_value=False),
        create_registration_session=Mock(),
        update_registration_session=Mock(),
        delete_registration_session=Mock(),
    )


def _cog(db):
    bot = Mock(spec=commands.Bot)
    bot.db = db
    return reg.ImmigrationCog(bot)


def _ctx(author):
    # A non-TextChannel channel object -> channel_id records as None.
    return SimpleNamespace(author=author, channel=Mock(), reply=AsyncMock())


class TestRegisterCommand:
    """Real ``!register`` callback with mocked Discord objects.

    The callback is driven with ``asyncio.run`` so the suite stays
    free of an async-test plugin dependency.
    """

    def test_bare_register_rejected_for_regular_member(self):
        guild = _guild([])
        member = _member(guild, [], user_id=5)
        db = _db()
        cog = _cog(db)
        ctx = _ctx(member)

        asyncio.run(cog.cmd_register.callback(cog, ctx))

        db.create_registration_session.assert_not_called()
        text = ctx.reply.await_args_list[0][0][0]
        assert "!register @user [city]" in text
        assert "!arrival" in text

    def test_bare_register_rejected_for_officer(self):
        guild = _guild(["Immigration Officer"])
        officer = _member(guild, ["Immigration Officer"], user_id=11)
        db = _db()
        cog = _cog(db)
        ctx = _ctx(officer)

        asyncio.run(cog.cmd_register.callback(cog, ctx))

        db.create_registration_session.assert_not_called()
        text = ctx.reply.await_args_list[0][0][0]
        assert "!register @user [city]" in text

    def test_officer_register_declines_existing_citizen(self):
        guild = _guild(["Immigration Officer"])
        officer = _member(guild, ["Immigration Officer"], user_id=11)
        target = _member(guild, [], user_id=7)
        db = _db()
        db.get_citizen.return_value = {"id": "DC-0007", "first_name": "Ada"}
        cog = _cog(db)
        ctx = _ctx(officer)

        asyncio.run(cog.cmd_register.callback(cog, ctx, member=target))

        db.create_registration_session.assert_not_called()
        text = ctx.reply.await_args_list[0][0][0]
        assert "already registered as a Delta City citizen" in text

    def test_officer_register_declines_active_session(self):
        guild = _guild(["Immigration Officer"])
        officer = _member(guild, ["Immigration Officer"], user_id=11)
        target = _member(guild, [], user_id=9)
        db = _db()
        db.has_active_registration.return_value = True
        cog = _cog(db)
        ctx = _ctx(officer)

        asyncio.run(cog.cmd_register.callback(cog, ctx, member=target))

        db.create_registration_session.assert_not_called()
        text = ctx.reply.await_args_list[0][0][0]
        assert "already has a registration in progress" in text

    def test_officer_can_register_another_member(self):
        guild = _guild(["Immigration Officer"])
        officer = _member(guild, ["Immigration Officer"], user_id=11)
        target = _member(guild, [], user_id=12)
        db = _db()
        cog = _cog(db)
        ctx = _ctx(officer)

        asyncio.run(cog.cmd_register.callback(cog, ctx, member=target))

        db.get_citizen.assert_called_once_with(12)
        args, kwargs = db.create_registration_session.call_args
        assert args[0] == 12  # session belongs to the target, not the officer
        assert kwargs["stage"] == "city"
        intro = ctx.reply.await_args_list[0][0][0]
        assert "<@12>" in intro
        assert "Immigration Officer has opened" in intro

    def test_officer_register_with_city_presets_community_stage(self):
        guild = _guild(["Immigration Officer"])
        officer = _member(guild, ["Immigration Officer"], user_id=11)
        target = _member(guild, [], user_id=12)
        db = _db()
        cog = _cog(db)
        ctx = _ctx(officer)

        asyncio.run(cog.cmd_register.callback(cog, ctx, member=target, city="Asaba"))

        args, kwargs = db.create_registration_session.call_args
        assert args[0] == 12
        assert kwargs["stage"] == "community"
        # City is persisted right after session creation.
        city_args, _ = db.update_registration_session.call_args
        assert city_args[0] == 12

    def test_regular_member_cannot_register_another(self):
        guild = _guild([])
        author = _member(guild, [], user_id=5)
        target = _member(guild, [], user_id=6)
        db = _db()
        cog = _cog(db)
        ctx = _ctx(author)

        asyncio.run(cog.cmd_register.callback(cog, ctx, member=target))

        db.create_registration_session.assert_not_called()
        text = ctx.reply.await_args_list[0][0][0]
        assert "can run !register" in text