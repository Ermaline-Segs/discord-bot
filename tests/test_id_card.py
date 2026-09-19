"""Unit tests for delta_city.id_card (PNG citizen ID card rendering)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from delta_city import id_card
from delta_city.id_card import (
    CARD_H,
    CARD_W,
    _format_registration_date,
    make_qr_reference,
    render_id_card,
)
from delta_city.portrait import generate_portrait

PORT = generate_portrait("DC-ASB-0001")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_qr_reference_contains_only_citizen_id():
    ref = make_qr_reference("DC-WAR-0042")
    assert ref == "DELTA-CITY|CITIZEN|DC-WAR-0042"


def test_qr_reference_is_stable():
    assert make_qr_reference("DC-ASB-0001") == make_qr_reference("DC-ASB-0001")


def test_format_registration_date():
    assert _format_registration_date(None) == "N/A"
    assert _format_registration_date("") == "N/A"
    assert _format_registration_date("2026-09-18") == "18/09/2026"
    assert _format_registration_date("2026-09-18T12:34:56") == "18/09/2026"
    assert _format_registration_date("2026/09/18") == "18/09/2026"
    # Space-separated datetimes are not a known format and pass through.
    assert _format_registration_date("2026-09-18 12:34:56") == "2026-09-18 12:34:56"
    # Unknown formats pass through untouched.
    assert _format_registration_date("sometime soon") == "sometime soon"


# ---------------------------------------------------------------------------
# render_id_card
# ---------------------------------------------------------------------------

def _load_png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def test_render_id_card_returns_valid_png():
    record = {
        "citizen_id": "DC-ASB-0001",
        "name": "Chukwudi Okafor",
        "city": "Asaba",
        "community": "Anioma",
        "gender": "Male",
        "nationality": "Deltaian",
        "status": "ACTIVE",
        "registration_date": "2026-09-18",
    }
    png = render_id_card(record, PORT)
    assert isinstance(png, bytes)
    img = _load_png(png)
    assert img.size == (CARD_W, CARD_H)


def test_render_id_card_writes_output_file(tmp_path):
    out = tmp_path / "card.png"
    record = {
        "citizen_id": "DC-WAR-0001",
        "name": "Somebody",
        "city": "Warri",
        "community": "Itsekiri",
        "gender": "Female",
        "nationality": "Deltaian",
        "status": "ACTIVE",
        "registration_date": None,
    }
    png = render_id_card(record, PORT, output_path=out)
    assert out.exists()
    assert out.read_bytes() == png
    assert _load_png(out.read_bytes()).size == (CARD_W, CARD_H)


def test_render_id_card_accepts_object_record(tmp_path):
    class Record:
        citizen_id = "DC-AGB-0001"
        name = "Obj Citizen"
        city = "Agbor"
        community = "Ika"
        gender = "Other"
        nationality = "Deltaian"
        status = "ACTIVE"
        registration_date = "2026-01-02"

    png = render_id_card(Record(), PORT)
    img = _load_png(png)
    assert img.size == (CARD_W, CARD_H)


def test_render_id_card_fills_missing_fields_safely():
    png = render_id_card({"citizen_id": "DC-OZO-0001"}, PORT)
    img = _load_png(png)
    assert img.size == (CARD_W, CARD_H)


def test_render_id_card_with_empty_portrait_still_renders():
    blank = Image.new("RGB", (512, 512), (200, 200, 200))
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    png = render_id_card(
        {"citizen_id": "DC-KWA-0001", "name": "No Portrait", "city": "Kwale",
         "community": "Ndokwa", "gender": "Female", "nationality": "Deltaian",
         "status": "ACTIVE", "registration_date": "2026-05-05"},
        buf.getvalue(),
    )
    assert _load_png(png).size == (CARD_W, CARD_H)