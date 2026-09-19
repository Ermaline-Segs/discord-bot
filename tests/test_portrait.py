"""Unit tests for delta_city.portrait (Pillow-based deterministic portraits)."""

import struct
import zlib

from PIL import Image
from io import BytesIO

from delta_city import portrait


def _png_size(png_bytes: bytes) -> tuple[int, int]:
    """Read width/height from a PNG's IHDR chunk."""
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width, height = struct.unpack(">II", png_bytes[16:24])
    return width, height


def test_generate_portrait_returns_valid_png():
    data = portrait.generate_portrait("DC-ASB-0001")
    assert data
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(BytesIO(data))
    assert img.size == (portrait.SIZE, portrait.SIZE)


def test_portrait_is_deterministic():
    first = portrait.generate_portrait("DC-WAR-0042")
    second = portrait.generate_portrait("DC-WAR-0042")
    assert first == second


def test_portrait_varies_by_citizen_id():
    # Different IDs should not produce byte-identical portraits.
    asaba = portrait.generate_portrait("DC-ASB-0001")
    warri = portrait.generate_portrait("DC-WAR-0001")
    assert asaba != warri


def test_generate_portrait_writes_output_path(tmp_path):
    target = tmp_path / "DC-ASB-0001.png"
    data = portrait.generate_portrait("DC-ASB-0001", output_path=target)
    assert target.exists()
    assert target.read_bytes() == data
    assert _png_size(target.read_bytes()) == (portrait.SIZE, portrait.SIZE)


def test_generate_portrait_respects_size():
    data = portrait.generate_portrait("DC-UGH-0001", size=256)
    img = Image.open(BytesIO(data))
    assert img.size == (256, 256)


def test_external_portrait_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("PORTRAIT_API_URL", raising=False)
    monkeypatch.delenv("PORTRAIT_API_KEY", raising=False)
    assert portrait._external_portrait("DC-ASB-0001", portrait.SIZE) is None


def test_generate_portrait_falls_back_when_external_fails(monkeypatch):
    """A failing image API must not break registration — Pillow fallback runs."""
    monkeypatch.setenv("PORTRAIT_API_URL", "http://127.0.0.1:9/unreachable")
    monkeypatch.delenv("PORTRAIT_API_KEY", raising=False)
    data = portrait.generate_portrait("DC-OZO-0001")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert _png_size(data) == (portrait.SIZE, portrait.SIZE)


def test_external_portrait_posts_and_returns_bytes(monkeypatch, tmp_path):
    """When the API is configured and succeeds, its bytes are used."""
    # Build a real PNG so downstream consumers never see garbage.
    sample = tmp_path / "sample.png"
    Image.new("RGB", (64, 64), (120, 140, 160)).save(sample)
    payload = sample.read_bytes()

    calls: list[dict] = []

    class FakeResponse:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=30):
        calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "data": req.data,
                "headers": dict(req.header_items()),
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setenv("PORTRAIT_API_URL", "https://img.example/endpoint")
    monkeypatch.setenv("PORTRAIT_API_KEY", "secret-key")
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = portrait._external_portrait("DC-KWA-0002", 512)
    assert result == payload
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://img.example/endpoint"
    assert call["method"] == "POST"
    assert call["headers"].get("Authorization") == "Bearer secret-key"
    body = call["data"].decode("utf-8")
    assert "DC-KWA-0002" in body
    assert "Fictional ID-card portrait" in body


def test_external_portrait_returns_none_on_error(monkeypatch):
    monkeypatch.setenv("PORTRAIT_API_URL", "https://img.example/endpoint")
    monkeypatch.delenv("PORTRAIT_API_KEY", raising=False)

    def fake_urlopen(req, timeout=30):
        raise RuntimeError("boom")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert portrait._external_portrait("DC-AGB-0001", portrait.SIZE) is None