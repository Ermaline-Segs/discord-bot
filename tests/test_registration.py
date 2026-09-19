"""Tests for the Delta City registration session machinery (pure logic).

The Discord view itself is exercised indirectly here through the render
helpers and the session state machine; the cog wiring is covered by
import-time validation in the bot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import delta_city.registration as reg
from delta_city import identity


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