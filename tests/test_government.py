"""Tests for the DELTA CITY government command logic (database-backed)."""
import pytest

from database.database import DeltaCityDB  # noqa: F401


def _register(db, discord_id, name, state="Asaba", gender="Female", status="Active"):
    db.register_citizen(
        discord_id,
        name=name,
        gender=gender,
        state=state,
        status=status,
    )


def test_appoint_and_holder(db):
    _register(db, 1, "Ada")
    _register(db, 2, "Chidi")
    db.log_appointment(1, "President", actor_id=2)

    holder = db.appointment_holder("President")
    assert holder is not None
    assert holder["discord_id"] == "1"
    assert holder["role_name"] == "President"


def test_unique_office_replacement(db):
    """A unique office can only be held by one citizen at a time."""
    _register(db, 1, "Ada")
    _register(db, 2, "Chidi")
    db.log_appointment(1, "President", actor_id=1)
    # The cog records the new holder and then voids the previous one
    # (see the unique-office branch in commands/government.py).
    db.log_appointment(2, "President", actor_id=2)
    db.void_appointment(1, "President", actor_id=2)

    holder = db.appointment_holder("President")
    assert holder["discord_id"] == "2"
    # Only one active President row remains.
    presidents = [a for a in db.active_appointments() if a["role_name"] == "President"]
    assert len(presidents) == 1


def test_multi_office_many_holders(db):
    """A multi office can be held by several citizens simultaneously."""
    for uid, name in ((1, "Ada"), (2, "Chidi"), (3, "Ngozi")):
        _register(db, uid, name, state="Warri")
        db.log_appointment(uid, "Senator", actor_id=1)

    senators = [a for a in db.active_appointments() if a["role_name"] == "Senator"]
    assert len(senators) == 3
    # appointment_holder returns an active holder and does not clear the others.
    holder = db.appointment_holder("Senator")
    assert holder is not None
    assert holder["discord_id"] in {"1", "2", "3"}


def test_state_offices_scoped_per_state(db):
    for uid in (1, 2):
        _register(db, uid, f"Gov{uid}")
        db.log_appointment(uid, "Governor of Asaba", state="Asaba", actor_id=1)
    _register(db, 3, "GovWarri", state="Warri")
    db.log_appointment(3, "Governor of Asaba", state="Warri", actor_id=1)

    asaba = db.appointment_holder("Governor of Asaba", state="Asaba")
    warri = db.appointment_holder("Governor of Asaba", state="Warri")
    assert asaba["discord_id"] in {"1", "2"}
    assert warri["discord_id"] == "3"


def test_reappoint_same_user_replaces_row(db):
    _register(db, 1, "Ada")
    db.log_appointment(1, "President", actor_id=1)
    db.log_appointment(1, "President", actor_id=1)
    presidents = [a for a in db.active_appointments() if a["role_name"] == "President"]
    assert len(presidents) == 1


def test_dismiss_not_holding_is_noop(db):
    _register(db, 1, "Ada")
    _register(db, 2, "Chidi")
    db.log_appointment(1, "President", actor_id=1)
    # Dismissing a user that holds nothing should not touch the real holder.
    assert db.void_appointment(2, "President", actor_id=1) is False
    holder = db.appointment_holder("President")
    assert holder is not None and holder["discord_id"] == "1"


def test_dismiss_clears_holder(db):
    _register(db, 1, "Ada")
    db.log_appointment(1, "President", actor_id=1)
    assert db.void_appointment(1, "President", actor_id=1) is True
    assert db.appointment_holder("President") is None


def test_active_appointments_for_user(db):
    _register(db, 1, "Ada")
    db.log_appointment(1, "President", actor_id=1)
    db.log_appointment(1, "Mayor of Asaba", state="Asaba", actor_id=1)
    rows = db.active_appointments(discord_id=1)
    assert len(rows) == 2