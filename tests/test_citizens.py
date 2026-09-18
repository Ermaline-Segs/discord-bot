"""Tests for the citizen registry: registration, IDs, search, edit, movement."""
import pytest

import config.settings as settings


# ---- registration & citizen IDs -------------------------------------------


def test_first_asaba_citizen_gets_first_id(db):
    row, created = db.register_citizen(101, "Ada Okafor", "Female", "Asaba")
    assert created is True
    assert row["citizen_id"] == "DC-ASB-0001"
    assert row["state"] == "Asaba"
    assert row["status"] == "Citizen"


def test_state_counters_are_independent(db):
    db.register_citizen(101, "Ada Okafor", "Female", "Asaba")
    db.register_citizen(202, "Blessing Eze", "Female", "Warri")

    asaba = db.get_citizen(101)
    Warri = db.get_citizen(202)
    assert asaba["citizen_id"] == "DC-ASB-0001"
    assert Warri["citizen_id"] == "DC-WAR-0001"


def test_second_citizen_in_same_state_gets_next_number(db):
    db.register_citizen(101, "Ada Okafor", "Female", "Asaba")
    row, created = db.register_citizen(303, "Chinedu Obi", "Male", "Asaba")
    assert created is True
    assert row["citizen_id"] == "DC-ASB-0002"


def test_registering_twice_returns_existing_profile(db):
    db.register_citizen(101, "Ada Okafor", "Female", "Asaba", status="Citizen")
    row, created = db.register_citizen(101, "New Name", "Male", "Warri")

    assert created is False
    assert row["citizen_id"] == "DC-ASB-0001"
    assert row["name"] == "Ada Okafor"  # profile is never overwritten
    assert db.citizen_count() == 1


def test_id_preserved_after_inactivation(db):
    """ID is permanent even if the citizen goes Inactive and re-registers."""
    db.register_citizen(101, "Ada Okafor", "Female", "Asaba")
    db.set_status(101, "Inactive", actor_id=1)
    row, created = db.register_citizen(101, "Ada Okafor", "Female", "Asaba")
    assert created is False
    assert row["citizen_id"] == "DC-ASB-0001"


def test_register_with_unknown_state_raises(db):
    with pytest.raises(ValueError):
        db.register_citizen(101, "Ada", "Female", "Atlantis")


# ---- lookups & search -------------------------------------------------------


def test_get_citizen_by_discord_id_and_citizen_id(db):
    db.register_citizen(101, "Ada Okafor", "Female", "Asaba")
    assert db.get_citizen(101)["name"] == "Ada Okafor"
    assert db.get_citizen_by_id("DC-ASB-0001")["discord_id"] == "101"
    assert db.get_citizen(999) is None
    assert db.get_citizen_by_id("DC-ASB-9999") is None


def test_search_citizens(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    db.register_citizen(2, "Blessing Eze", "Female", "Warri")
    db.register_citizen(3, "Chinedu Obi", "Male", "Asaba")

    hits = db.search_citizens("Okafor")
    assert len(hits) == 1
    assert hits[0]["name"] == "Ada Okafor"

    assert db.search_citizens("zzz-not-a-name") == []


def test_all_citizens_and_counts(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    db.register_citizen(2, "Blessing Eze", "Female", "Warri")
    db.register_citizen(3, "Chinedu Obi", "Male", "Asaba", status="Resident")

    assert db.citizen_count() == 3
    assert db.citizen_count(state="Asaba") == 2
    assert len(db.all_citizens()) == 3


# ---- editing ----------------------------------------------------------------


def test_edit_updates_fields_and_audits(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    changes = db.update_citizen(1, actor_id=9, name="Ada Nwosu", status="Resident")

    keys = [c[0] for c in changes]
    assert keys == ["name", "status"]
    assert changes[0] == ("name", "Ada Okafor", "Ada Nwosu")

    row = db.get_citizen(1)
    assert row["name"] == "Ada Nwosu"
    assert row["status"] == "Resident"

    edits = [a for a in db.recent_audit() if a["action"] == "edit"]
    assert edits  # at least one edit audit entry


def test_edit_noop_returns_empty_changes(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    assert db.update_citizen(1, name="Ada Okafor") == []


def test_edit_ignores_unknown_keys(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    assert db.update_citizen(1, citizen_id="DC-EVIL-9999") == []
    assert db.get_citizen(1)["citizen_id"] == "DC-ASB-0001"


def test_edit_missing_citizen_returns_empty(db):
    assert db.update_citizen(999, name="Ghost") == []


# ---- state movement ----------------------------------------------------------


def test_move_changes_state_but_keeps_citizen_id(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    moved = db.change_state(1, "Warri", actor_id=9)

    assert moved is True
    row = db.get_citizen(1)
    assert row["state"] == "Warri"
    assert row["citizen_id"] == "DC-ASB-0001"  # ID is permanent

    moves = [a for a in db.recent_audit() if a["action"] == "move"]
    assert moves and "Warri" in moves[0]["details"]


def test_move_to_same_state_is_noop(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    assert db.change_state(1, "Asaba") is False


def test_move_unknown_citizen_is_noop(db):
    assert db.change_state(999, "Warri") is False


def test_move_to_unknown_state_raises(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    with pytest.raises(ValueError):
        db.change_state(1, "Atlantis")


def test_get_citizens_in_state(db):
    db.register_citizen(1, "Ada Okafor", "Female", "Asaba")
    db.register_citizen(2, "Blessing Eze", "Female", "Warri")
    asaba = db.get_citizens_in_state("Asaba")
    assert len(asaba) == 1
    assert asaba[0]["discord_id"] == "1"


# ---- helpers -----------------------------------------------------------------


def test_make_citizen_id_format_and_validation(db):
    assert db.make_citizen_id("Asaba", 7) == "DC-ASB-0007"
    with pytest.raises(ValueError):
        db.make_citizen_id("Atlantis", 1)


def test_next_citizen_number_sequential_and_atomic(db):
    assert db.next_citizen_number("Asaba") == 1
    assert db.next_citizen_number("Asaba") == 2
    assert db.next_citizen_number("Warri") == 1  # independent counter


def test_settings_integrity():
    assert len(settings.STATES) == 6
    assert "President" in settings.NATIONAL_ROLES
    assert "Minister" in settings.NATIONAL_MULTI_ROLES
    assert "Governor" in settings.STATE_ROLES
    assert "Commissioner" in settings.STATE_MULTI_ROLES