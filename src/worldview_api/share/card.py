"""Render the 1200x630 share preview card with Pillow (no headless browser).

Cards are immutable per share id, so we render once and cache the PNG on disk;
the endpoint serves it with long-lived immutable cache headers. The look mirrors
the app's command-center palette (deep navy + cyan chrome).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import settings
from .store import Share

log = logging.getLogger(__name__)

W, H = 1200, 630
BG = (2, 4, 10)
CYAN = (76, 201, 255)
BRIGHT = (124, 224, 255)
TEXT = (207, 230, 255)
DIM = (120, 160, 200)
ACCENT = (255, 90, 74)

# Candidate TrueType fonts, in preference order. Falls back to Pillow's bitmap
# default if none are present (degraded but functional).
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "DejaVuSans.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "DejaVuSans.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _FONT_CANDIDATES if bold else _FONT_CANDIDATES_REGULAR
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and (cur or words):
        # Ellipsize the last line if we ran out of room.
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def render_card(share: Share) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Subtle frame + scanline vibe.
    d.rectangle([0, 0, W - 1, H - 1], outline=(20, 40, 60), width=2)
    for y in range(0, H, 3):
        d.line([(0, y), (W, y)], fill=(4, 8, 16))

    # Globe motif on the right.
    cx, cy, r = 980, 330, 210
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(40, 90, 130), width=2)
    for i in range(1, 4):
        rr = r * i / 4
        d.ellipse([cx - r, cy - rr, cx + r, cy + rr], outline=(24, 60, 90), width=1)
        d.ellipse([cx - rr, cy - r, cx + rr, cy + r], outline=(24, 60, 90), width=1)
    # A glowing marker if we have a fly-to.
    if share.fly_lat is not None and share.fly_lon is not None:
        mx = cx + int((share.fly_lon / 180.0) * r * 0.8)
        my = cy - int((share.fly_lat / 90.0) * r * 0.8)
        for rad, col in ((14, (255, 90, 74)), (7, (255, 150, 120)), (3, (255, 220, 200))):
            d.ellipse([mx - rad, my - rad, mx + rad, my + rad], fill=col)

    # Brand mark (drawn triangle + wordmark — no reliance on unicode glyphs).
    d.polygon([(60, 58), (88, 58), (60, 86)], fill=CYAN)
    d.text((100, 56), "WORLDVIEW", font=_font(34, bold=True), fill=BRIGHT)
    d.text((102, 96), "jarvisworlds.com", font=_font(20), fill=DIM)

    y = 190
    # Question / place line.
    label = share.question or share.title or share.place or "The world right now"
    # Keep prefixes ASCII — the bundled DejaVu font lacks decorative glyphs
    # (e.g. ◎) and would render them as tofu boxes.
    prefix = "ASK  " if share.question else ""
    for line in _wrap(d, (prefix + label) if prefix else label, _font(40, bold=True), 820, 2):
        d.text((60, y), line, font=_font(40, bold=True), fill=TEXT)
        y += 54

    # Divider.
    y += 8
    d.line([(60, y), (820, y)], fill=(40, 90, 130), width=2)
    y += 24

    # Answer.
    if share.answer:
        for line in _wrap(d, share.answer, _font(30), 820, 5):
            d.text((60, y), line, font=_font(30), fill=(180, 210, 240))
            y += 42

    # Stats footer.
    stats = share.stats or {}
    bits = []
    if share.place:
        bits.append(share.place)
    if stats.get("event_count"):
        bits.append(f"{stats['event_count']} events")
    if stats.get("sources"):
        bits.append(f"{stats['sources']} sources")
    if stats.get("sigma"):
        bits.append(f"{stats['sigma']}σ")
    if bits:
        d.text((60, H - 70), "   ·   ".join(bits), font=_font(22, bold=True), fill=CYAN)

    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_or_render_card_path(share: Share) -> Path:
    """Return the path to the cached PNG, rendering + writing it once if absent."""
    cache_dir = Path(settings.share_card_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{share.id}.png"
    if path.exists() and path.stat().st_size > 0:
        return path
    data = render_card(share)
    tmp = path.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path
