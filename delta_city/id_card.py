"""Pillow-based Delta City citizen identity card renderer.

Renders a government-styled PNG identity card from a citizen record plus
the portrait PNG produced by :mod:`delta_city.portrait`.

The card is deliberately and visibly marked as a *fictional digital*
identity document so it cannot be mistaken for a real government ID.

Public API
----------
``render_id_card(record, portrait_png, output_path=None) -> bytes``
    *record* is a mapping with (at least) these keys::

        citizen_id, name, city, community, gender,
        nationality, status, registration_date

    ``registration_date`` is a string in ``YYYY-MM-DD`` (or ISO) format;
    it is displayed as ``DD/MM/YYYY``.

``make_qr_reference(citizen_id) -> str``
    The verification reference encoded into the card's QR code.
"""

from __future__ import annotations

import io
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Card geometry & palette
# ---------------------------------------------------------------------------

CARD_W = 1712
CARD_H = 1080

# Government-style palette (fictional Delta City).
COL_BG = (250, 250, 248)
COL_HEADER = (0, 84, 49)        # deep green
COL_HEADER_DARK = (0, 60, 36)
COL_GOLD = (201, 163, 62)
COL_TEXT = (24, 28, 33)
COL_LABEL = (96, 102, 112)
COL_LINE = (208, 210, 214)
COL_BADGE_GREEN = (26, 122, 66)
COL_BADGE_RED = (158, 40, 40)
COL_DISCLAIMER_BG = (238, 236, 228)
COL_DISCLAIMER_TEXT = (84, 78, 62)

_MARGIN = 24
_HEADER_H = 170
_PORTRAIT = 430
_PORTRAIT_X = _MARGIN + 36
_PORTRAIT_Y = _HEADER_H + 60
_FIELDS_X = _PORTRAIT_X + _PORTRAIT + 56
_QR_SIZE = 300


# ---------------------------------------------------------------------------
# Fonts (no system fonts required — works headless)
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Best-available font for the card, degrading gracefully."""
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Pillow >= 10.1 ships a scalable built-in default font.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# QR reference
# ---------------------------------------------------------------------------

def make_qr_reference(citizen_id: str) -> str:
    """Build the safe public verification reference for the QR code.

    Only the Citizen ID is encoded — never the Discord user ID.
    """
    return f"DELTA-CITY|CITIZEN|{citizen_id}"


def _make_qr_image(reference: str, size: int = _QR_SIZE) -> Image.Image:
    """Render a QR code image with a quiet zone, square pixels."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=3,
    )
    qr.add_data(reference)
    qr.make(fit=True)
    img = qr.make_image(fill_color=(20, 24, 28), back_color=(255, 255, 255)).convert("RGB")
    img = img.resize((size, size), Image.NEAREST)
    return img


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _format_registration_date(value) -> str:
    """Render a YYYY-MM-DD / ISO date as DD/MM/YYYY (falls back safely)."""
    if not value:
        return "N/A"
    text = str(value).strip()
    try:
        from datetime import datetime
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(text[:19], fmt)
                return dt.strftime("%d/%m/%Y")
            except ValueError:
                continue
    except Exception:
        pass
    return text


def _draw_field_row(draw: ImageDraw.ImageDraw, x: int, y: int,
                    label: str, value: str, value_font,
                    max_width: int) -> int:
    """Draw one label+value row; return the y position after the row."""
    label_font = _font(26)
    draw.text((x, y), label, font=label_font, fill=COL_LABEL)
    value_y = y + 34
    # Truncate long values that would overflow the card.
    text = str(value) if value is not None else "N/A"
    if text:
        width = draw.textlength(text, font=value_font)
        while width > max_width and len(text) > 4:
            text = text[:-1]
            width = draw.textlength(text, font=value_font)
        if text != str(value):
            text = text.rstrip() + "…"
    draw.text((x, value_y), text if text else "N/A", font=value_font, fill=COL_TEXT)
    return value_y + 62


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_id_card(record, portrait_png: bytes,
                   output_path: str | Path | None = None) -> bytes:
    """Render the citizen identity card and return the PNG bytes.

    Parameters
    ----------
    record:
        Mapping (or object with the same attributes) containing at least
        ``citizen_id``, ``name``, ``city``, ``community``, ``gender``,
        ``nationality``, ``status`` and ``registration_date``.
    portrait_png:
        Raw PNG bytes of the citizen portrait (see
        :func:`delta_city.portrait.generate_portrait`).
    output_path:
        Optional path — the PNG is also written there.
    """
    def field(key: str) -> str:
        if isinstance(record, dict):
            value = record.get(key)
        else:
            value = getattr(record, key, None)
        return "" if value is None else str(value)

    citizen_id = field("citizen_id")
    name = field("name") or "UNKNOWN"
    city = field("city") or field("state") or "N/A"
    community = field("community") or field("heritage") or "N/A"
    gender = field("gender") or "N/A"
    nationality = field("nationality") or "Deltaian"
    status = field("status") or "ACTIVE"
    reg_date = _format_registration_date(field("registration_date"))

    img = Image.new("RGB", (CARD_W, CARD_H), COL_BG)
    draw = ImageDraw.Draw(img)

    # ---- outer frame ------------------------------------------------------
    draw.rectangle((_MARGIN, _MARGIN, CARD_W - _MARGIN, CARD_H - _MARGIN),
                   outline=COL_GOLD, width=4)
    draw.rectangle((_MARGIN + 10, _MARGIN + 10, CARD_W - _MARGIN - 10, CARD_H - _MARGIN - 10),
                   outline=COL_HEADER, width=2)

    # ---- header band -------------------------------------------------------
    header_bottom = _MARGIN + 10 + _HEADER_H
    draw.rectangle((_MARGIN + 10, _MARGIN + 10, CARD_W - _MARGIN - 10, header_bottom),
                   fill=COL_HEADER)
    draw.rectangle((_MARGIN + 10, header_bottom - 14, CARD_W - _MARGIN - 10, header_bottom),
                   fill=COL_GOLD)

    # Emblem: concentric circles with "DC"
    ex, ey, er = _MARGIN + 10 + 88, (_MARGIN + 10 + header_bottom) // 2, 62
    draw.ellipse((ex - er, ey - er, ex + er, ey + er), fill=COL_GOLD)
    draw.ellipse((ex - er + 8, ey - er + 8, ex + er - 8, ey + er - 8), fill=COL_HEADER_DARK)
    emblem_font = _font(52, bold=True)
    ew = draw.textlength("DC", font=emblem_font)
    draw.text((ex - ew / 2, ey - 34), "DC", font=emblem_font, fill=(255, 250, 235))

    # Header text
    title_font = _font(64, bold=True)
    draw.text((ex + er + 40, _MARGIN + 10 + 26), "DELTA CITY",
              font=title_font, fill=(255, 255, 255))
    sub_font = _font(34, bold=True)
    draw.text((ex + er + 42, _MARGIN + 10 + 104), "CITIZEN IDENTITY CARD",
              font=sub_font, fill=(222, 226, 230))

    # Citizen ID top-right in header
    cid_font = _font(30, bold=True)
    cid_w = draw.textlength(citizen_id, font=cid_font)
    draw.text((CARD_W - _MARGIN - 10 - 26 - cid_w, _MARGIN + 10 + 44),
              citizen_id, font=cid_font, fill=COL_GOLD)
    doc_font = _font(22)
    draw.text((CARD_W - _MARGIN - 10 - 26 - cid_w, _MARGIN + 10 + 86),
              "OFFICIAL RECORD NO. 001", font=doc_font, fill=(200, 206, 214))

    # ---- portrait ----------------------------------------------------------
    px, py = _PORTRAIT_X, _PORTRAIT_Y
    portrait = Image.open(io.BytesIO(portrait_png)).convert("RGB")
    portrait = portrait.resize((_PORTRAIT, _PORTRAIT), Image.LANCZOS)
    img.paste(portrait, (px, py))
    draw.rectangle((px - 4, py - 4, px + _PORTRAIT + 4, py + _PORTRAIT + 4),
                   outline=COL_HEADER, width=3)
    draw.rectangle((px - 10, py - 10, px + _PORTRAIT + 10, py + _PORTRAIT + 10),
                   outline=COL_GOLD, width=2)

    # ---- field rows --------------------------------------------------------
    rows = [
        ("NAME", name, _font(44, bold=True)),
        ("CITIZEN ID", citizen_id, _font(40, bold=True)),
        ("CITY", city, _font(38, bold=True)),
        ("COMMUNITY / HERITAGE", community, _font(36, bold=True)),
        ("GENDER", gender, _font(36, bold=True)),
        ("NATIONALITY", nationality, _font(36, bold=True)),
    ]
    y = _PORTRAIT_Y + 6
    max_w = CARD_W - _MARGIN - _FIELDS_X - 460  # leave room for QR column
    for label, value, vfont in rows:
        y = _draw_field_row(draw, _FIELDS_X, y, label, value, vfont, max_w)

    # Status badge
    badge_font = _font(34, bold=True)
    status_up = status.upper()
    badge_colour = COL_BADGE_GREEN if "ACTIVE" in status_up else COL_BADGE_RED
    bw = draw.textlength(status_up, font=badge_font) + 56
    bx, by = _FIELDS_X, y + 8
    draw.rounded_rectangle((bx, by, bx + bw, by + 60), radius=30, fill=badge_colour)
    draw.text((bx + 28, by + 12), status_up, font=badge_font, fill=(255, 255, 255))
    status_label_font = _font(24)
    draw.text((bx + bw + 24, by + 18), "STATUS", font=status_label_font, fill=COL_LABEL)

    # ---- registration date + QR -------------------------------------------
    date_y = by + 96
    date_label_font = _font(24)
    date_font = _font(36, bold=True)
    draw.text((_FIELDS_X, date_y), "REGISTRATION DATE", font=date_label_font, fill=COL_LABEL)
    draw.text((_FIELDS_X, date_y + 32), reg_date, font=date_font, fill=COL_TEXT)

    qr = _make_qr_image(make_qr_reference(citizen_id))
    img.paste(qr, (CARD_W - _MARGIN - 10 - 20 - _QR_SIZE, _PORTRAIT_Y + 40))
    qr_x = CARD_W - _MARGIN - 10 - 20 - _QR_SIZE
    qr_font = _font(22)
    qr_w = draw.textlength("VERIFY", font=qr_font)
    draw.text((qr_x + (_QR_SIZE - qr_w) / 2, _PORTRAIT_Y + 40 + _QR_SIZE + 12),
              "VERIFY", font=qr_font, fill=COL_LABEL)

    # ---- fictional-document disclaimer band --------------------------------
    band_top = CARD_H - _MARGIN - 10 - 74
    draw.rectangle((_MARGIN + 10, band_top, CARD_W - _MARGIN - 10, CARD_H - _MARGIN - 10),
                   fill=COL_DISCLAIMER_BG)
    disc_font = _font(26, bold=True)
    line1 = "FICTIONAL DIGITAL IDENTITY DOCUMENT — DELTA CITY (DTC-DCI)"
    line2 = "This card is issued by the fictional Delta City government for community use only. " \
            "It is not a real government identity document."
    for i, line in enumerate((line1, line2)):
        w = draw.textlength(line, font=disc_font)
        draw.text(((CARD_W - w) / 2, band_top + 12 + i * 34), line,
                  font=disc_font, fill=COL_DISCLAIMER_TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)

    return png_bytes


__all__ = ["render_id_card", "make_qr_reference", "CARD_W", "CARD_H"]