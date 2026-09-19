"""Deterministic Pillow-based portrait generator for Delta City citizens.

Produces a stylised, geometric head-and-shoulders portrait suitable for an
ID card.  The output is always the same for a given *citizen_id*, making it
reproducible across bot restarts.

The module is structured so an external image-generation API can be plugged
in later without changing any callers.  Set the environment variables
``PORTRAIT_API_URL`` (endpoint that accepts POST and returns an image) and
``PORTRAIT_API_KEY`` (sent as ``Authorization: Bearer <key>``) to enable it;
any failure in the external call transparently falls back to the
deterministic Pillow path below.  Credentials are never hard-coded.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw

# Portrait dimensions (square, suitable for ID card).
SIZE = 512

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_from_id(citizen_id: str) -> int:
    """Return a deterministic integer seed from a Citizen ID string."""
    h = hashlib.sha256(citizen_id.encode()).hexdigest()
    return int(h[:8], 16)


class _Rng:
    """Tiny deterministic RNG seeded from a Citizen ID."""

    def __init__(self, seed: int) -> None:
        self._state = seed

    def _next(self) -> int:
        self._state = (self._state * 1103515245 + 12345) & 0x7FFFFFFF
        return self._state

    def randint(self, lo: int, hi: int) -> int:
        return lo + self._next() % (hi - lo + 1)

    def choice(self, seq):
        return seq[self._next() % len(seq)]

    def uniform(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * (self._next() / 0x7FFFFFFF)


# ---------------------------------------------------------------------------
# Colour palettes
# ---------------------------------------------------------------------------

_SKIN_TONES = [
    (222, 184, 150), (210, 165, 130), (195, 150, 115),
    (175, 130, 100), (160, 115, 85),  (240, 210, 180),
    (205, 170, 140), (185, 140, 110), (150, 105, 75),
    (135, 95, 65),
]

_HAIR_COLOURS = [
    (25, 20, 15),   # black
    (45, 30, 20),   # very dark brown
    (60, 35, 15),   # dark brown
    (80, 50, 25),   # brown
    (110, 70, 35),  # medium brown
    (30, 25, 20),   # near-black
]

_SHIRT_COLOURS = [
    (30, 60, 120),   # navy
    (45, 90, 50),    # forest green
    (120, 35, 35),   # dark red
    (60, 60, 70),    # charcoal
    (100, 80, 50),   # olive/brown
    (50, 50, 90),    # dark blue-grey
    (85, 35, 60),    # burgundy
    (35, 70, 80),    # teal
]

_BG_COLOURS = [
    (220, 225, 235),  # light blue-grey
    (230, 228, 220),  # warm grey
    (215, 225, 215),  # light sage
    (225, 220, 230),  # lavender-grey
    (235, 225, 215),  # warm beige
]

_EYE_COLOURS = [
    (40, 30, 20),   # dark brown
    (60, 40, 25),   # brown
    (50, 35, 20),   # hazel-dark
    (35, 25, 15),   # near-black
]


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _draw_head(draw: ImageDraw.ImageDraw, rng: _Rng, skin: tuple,
               cx: int, cy: int) -> dict:
    """Draw the head (ellipse) and return landmark positions."""
    hw = rng.randint(58, 68)   # half-width
    hh = rng.randint(72, 85)   # half-height
    head_bbox = (cx - hw, cy - hh, cx + hw, cy + hh)
    draw.ellipse(head_bbox, fill=skin)

    # Subtle cheek shading — slightly darker ellipses
    shade = tuple(max(0, c - 18) for c in skin)
    cheek_r = rng.randint(14, 20)
    for dx in (-1, 1):
        bx = cx + dx * (hw - 12)
        by = cy + int(hh * 0.15)
        draw.ellipse((bx - cheek_r, by - cheek_r // 2,
                       bx + cheek_r, by + cheek_r // 2), fill=shade)

    return {"cx": cx, "cy": cy, "hw": hw, "hh": hh}


def _draw_hair(draw: ImageDraw.ImageDraw, rng: _Rng, colour: tuple,
               head: dict) -> None:
    """Draw hair on top of the head."""
    cx, cy, hw, hh = head["cx"], head["cy"], head["hw"], head["hh"]
    style = rng.randint(0, 4)

    if style == 0:
        # Short cropped — slightly larger dark ellipse above head
        top_h = rng.randint(10, 18)
        draw.ellipse(
            (cx - hw - 4, cy - hh - top_h, cx + hw + 4, cy - int(hh * 0.3)),
            fill=colour,
        )
    elif style == 1:
        # Side-parted — arc on top with a parting
        top_h = rng.randint(12, 22)
        draw.ellipse(
            (cx - hw - 6, cy - hh - top_h, cx + hw + 6, cy - int(hh * 0.2)),
            fill=colour,
        )
        # Parting line
        px = cx + rng.randint(-8, 8)
        draw.line(
            [(px, cy - hh - top_h + 4), (px + rng.randint(-4, 4), cy - int(hh * 0.35))],
            fill=colour, width=3,
        )
    elif style == 2:
        # Afro / round
        r = hw + rng.randint(16, 28)
        draw.ellipse(
            (cx - r, cy - hh - r + 8, cx + r, cy - int(hh * 0.1)),
            fill=colour,
        )
    elif style == 3:
        # Tall top — rectangular-ish
        top_h = rng.randint(20, 32)
        draw.rounded_rectangle(
            (cx - hw - 2, cy - hh - top_h, cx + hw + 2, cy - int(hh * 0.25)),
            radius=rng.randint(8, 16), fill=colour,
        )
    else:
        # Buzz cut — thin layer
        draw.ellipse(
            (cx - hw - 2, cy - hh - 6, cx + hw + 2, cy - int(hh * 0.35)),
            fill=colour,
        )


def _draw_eyes(draw: ImageDraw.ImageDraw, rng: _Rng, head: dict,
               eye_colour: tuple, skin: tuple) -> None:
    """Draw two simple eyes."""
    cx, cy, hw = head["cx"], head["cy"], head["hw"]
    eye_y = cy + rng.randint(-4, 4)
    eye_spacing = hw - rng.randint(10, 18)
    eye_w = rng.randint(7, 10)
    eye_h = rng.randint(4, 6)

    for dx in (-1, 1):
        ex = cx + dx * eye_spacing
        # White of the eye
        draw.ellipse(
            (ex - eye_w, eye_y - eye_h, ex + eye_w, eye_y + eye_h),
            fill=(240, 240, 245),
        )
        # Iris
        ir = eye_h - 1
        draw.ellipse(
            (ex - ir, eye_y - ir, ex + ir, eye_y + ir),
            fill=eye_colour,
        )
        # Pupil
        pr = max(2, ir - 2)
        draw.ellipse(
            (ex - pr, eye_y - pr, ex + pr, eye_y + pr),
            fill=(10, 10, 15),
        )
        # Highlight
        hx, hy = ex - pr // 2, eye_y - pr // 2
        draw.ellipse((hx - 1, hy - 1, hx + 2, hy + 2), fill=(255, 255, 255))

    # Eyebrows
    brow_y = eye_y - eye_h - rng.randint(5, 9)
    brow_w = eye_w + rng.randint(2, 5)
    brow_h = rng.randint(2, 3)
    brow_colour = tuple(max(0, c - 10) for c in eye_colour)
    for dx in (-1, 1):
        bx = cx + dx * eye_spacing
        draw.ellipse(
            (bx - brow_w, brow_y - brow_h, bx + brow_w, brow_y + brow_h),
            fill=brow_colour,
        )


def _draw_nose(draw: ImageDraw.ImageDraw, rng: _Rng, head: dict,
               skin: tuple) -> None:
    """Draw a simple nose."""
    cx, cy, hw = head["cx"], head["cy"], head["hw"]
    nose_y = cy + rng.randint(8, 16)
    nose_w = rng.randint(6, 10)
    shade = tuple(max(0, c - 20) for c in skin)
    # Simple triangular nose
    draw.polygon(
        [(cx, nose_y - rng.randint(8, 14)),
         (cx - nose_w, nose_y + 4),
         (cx + nose_w, nose_y + 4)],
        fill=shade,
    )


def _draw_mouth(draw: ImageDraw.ImageDraw, rng: _Rng, head: dict,
                skin: tuple) -> None:
    """Draw a neutral mouth."""
    cx, cy, hw = head["cx"], head["cy"], head["hw"]
    mouth_y = cy + rng.randint(28, 40)
    mouth_w = rng.randint(14, 22)
    mouth_h = rng.randint(4, 7)
    # Lip colour — slightly pink/redder than skin
    lip = (
        min(255, skin[0] + rng.randint(10, 30)),
        max(0, skin[1] - rng.randint(5, 15)),
        max(0, skin[2] - rng.randint(5, 15)),
    )
    # Open mouth ellipse (neutral expression)
    draw.ellipse(
        (cx - mouth_w, mouth_y - mouth_h, cx + mouth_w, mouth_y + mouth_h),
        fill=lip,
    )
    # Slight inner shadow
    inner = tuple(max(0, c - 30) for c in lip)
    draw.ellipse(
        (cx - mouth_w + 3, mouth_y - mouth_h + 2,
         cx + mouth_w - 3, mouth_y + mouth_h - 2),
        fill=inner,
    )


def _draw_neck(draw: ImageDraw.ImageDraw, rng: _Rng, head: dict,
               skin: tuple) -> None:
    """Draw the neck connecting head to shoulders."""
    cx, cy, hw, hh = head["cx"], head["cy"], head["hw"], head["hh"]
    neck_w = hw - rng.randint(8, 15)
    neck_top = cy + hh - 6
    neck_bot = cy + hh + rng.randint(22, 32)
    shade = tuple(max(0, c - 8) for c in skin)
    draw.rectangle(
        (cx - neck_w, neck_top, cx + neck_w, neck_bot),
        fill=shade,
    )


def _draw_shoulders(draw: ImageDraw.ImageDraw, rng: _Rng, head: dict,
                    shirt: tuple, skin: tuple) -> None:
    """Draw shoulders and upper torso (visible portion of a shirt)."""
    cx, cy, hh = head["cx"], head["cy"], head["hh"]
    shoulder_top = cy + hh + rng.randint(18, 26)
    shoulder_w = rng.randint(140, 170)
    bottom = SIZE + 10  # extend past the bottom of the image

    # Main shirt area
    draw.rounded_rectangle(
        (cx - shoulder_w, shoulder_top, cx + shoulder_w, bottom),
        radius=rng.randint(20, 40), fill=shirt,
    )

    # Collar / neckline — a V or U shape in a slightly lighter shade
    collar = tuple(min(255, c + 15) for c in shirt)
    collar_w = rng.randint(18, 28)
    collar_depth = rng.randint(16, 28)
    draw.polygon(
        [(cx - collar_w, shoulder_top),
         (cx, shoulder_top + collar_depth),
         (cx + collar_w, shoulder_top)],
        fill=collar,
    )

    # Exposed skin above the collar
    neck_bot = cy + hh + rng.randint(18, 26)
    draw.rectangle(
        (cx - collar_w + 4, neck_bot - 4, cx + collar_w - 4, shoulder_top + 2),
        fill=skin,
    )


def _draw_ears(draw: ImageDraw.ImageDraw, rng: _Rng, head: dict,
               skin: tuple) -> None:
    """Draw simple ears on the sides of the head."""
    cx, cy, hw, hh = head["cx"], head["cy"], head["hw"], head["hh"]
    ear_w = rng.randint(8, 13)
    ear_h = rng.randint(12, 18)
    ear_y = cy + rng.randint(-4, 4)
    shade = tuple(max(0, c - 10) for c in skin)

    for dx in (-1, 1):
        ex = cx + dx * (hw - 2)
        draw.ellipse(
            (ex - ear_w // 2, ear_y - ear_h, ex + ear_w // 2, ear_y + ear_h),
            fill=shade,
        )


# ---------------------------------------------------------------------------
# External image-generation API hook
# ---------------------------------------------------------------------------


def _external_portrait(citizen_id: str, size: int) -> bytes | None:
    """Optionally fetch a portrait from an external image-generation API.

    Credentials come from environment variables only:

    - ``PORTRAIT_API_URL`` — endpoint that accepts a JSON POST and returns
      an image (PNG/JPEG).
    - ``PORTRAIT_API_KEY`` — sent as ``Authorization: Bearer <key>``.

    Returns image bytes on success, or ``None`` when the hook is not
    configured or the call fails (the Pillow fallback then runs).
    """
    url = os.environ.get("PORTRAIT_API_URL", "").strip()
    api_key = os.environ.get("PORTRAIT_API_KEY", "").strip()
    if not url:
        return None
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            url,
            data=json.dumps(
                {
                    "citizen_id": citizen_id,
                    "size": size,
                    "prompt": (
                        "Fictional ID-card portrait, head and shoulders, "
                        "neutral expression, plain background, even lighting, "
                        "non-famous person, digital illustration."
                    ),
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            return None
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------

def generate_portrait(citizen_id: str, *, output_path: str | Path | None = None,
                      size: int = SIZE) -> bytes:
    """Generate a deterministic portrait for *citizen_id* and return PNG bytes.

    Parameters
    ----------
    citizen_id:
        The citizen's unique ID (e.g. ``DC-ASB-0001``).  Used as the seed
        so the same ID always produces the same image.
    output_path:
        If provided the PNG is also written to this path.
    size:
        Width and height in pixels (square).

    Returns
    -------
    bytes
        The raw PNG image data.
    """
    # Try the external image API (if configured); any failure falls back to
    # the deterministic Pillow path below.
    external = _external_portrait(citizen_id, size)
    if external is not None:
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(external)
        return external

    rng = _Rng(_seed_from_id(citizen_id))

    # Pick colours deterministically
    skin = rng.choice(_SKIN_TONES)
    hair = rng.choice(_HAIR_COLOURS)
    shirt = rng.choice(_SHIRT_COLOURS)
    bg = rng.choice(_BG_COLOURS)
    eye = rng.choice(_EYE_COLOURS)

    # Create image
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)

    cx = size // 2 + rng.randint(-10, 10)
    head_cy = int(size * 0.34) + rng.randint(-8, 8)

    head = _draw_head(draw, rng, skin, cx, head_cy)
    _draw_ears(draw, rng, head, skin)
    _draw_hair(draw, rng, hair, head)
    _draw_eyes(draw, rng, head, eye, skin)
    _draw_nose(draw, rng, head, skin)
    _draw_mouth(draw, rng, head, skin)
    _draw_neck(draw, rng, head, skin)
    _draw_shoulders(draw, rng, head, shirt, skin)

    # Subtle vignette-like border
    border = rng.randint(1, 2)
    draw.rectangle(
        (border, border, size - border - 1, size - border - 1),
        outline=(180, 180, 185), width=border,
    )

    # Encode to PNG bytes
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    # Optionally write to disk
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)

    return png_bytes
