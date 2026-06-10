"""Server-rendered share artifacts behind /s/<id>.

The apex is a static SPA, so pasted links can't unfurl on their own (crawlers
don't run JS). This package snapshots a shareable moment (store), serves
per-share OpenGraph HTML that redirects humans into the SPA deep link (html),
and renders an immutable 1200x630 PNG preview card (card).
"""

from __future__ import annotations

import re

# Control chars + anything that could break meta/markup when echoed into a card
# or an OG tag. We keep it conservative: collapse whitespace, drop angle
# brackets and control characters, then length-cap.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ANGLE_RE = re.compile(r"[<>]")


def sanitize_text(text: str | None, max_len: int = 200) -> str:
    """Sanitize + length-cap arbitrary user text before it's rasterized into a
    card or embedded in meta tags. Never returns None."""
    if not text:
        return ""
    t = _CONTROL_RE.sub(" ", text)
    t = _ANGLE_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t
