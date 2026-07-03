"""Persistent thumbnails for game cards.

Collections outlive the 3-day event retention window, so card images must be
cached locally at mint time. Failures are deliberately non-fatal: the card
renderer falls back to procedural art.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from uuid import UUID

import httpx
from PIL import Image, ImageOps

from ..config import settings

log = logging.getLogger(__name__)

_MAX_BYTES = 4_000_000
_MAX_WIDTH = 800
_JPEG_QUALITY = 80


def image_path(card_id: UUID | str) -> Path:
    return Path(settings.game_card_image_dir) / f"{card_id}.jpg"


def cache_card_image(card_id: UUID | str, url: str | None) -> bool:
    if not url:
        return False
    path = image_path(card_id)
    if path.is_file():
        return True
    try:
        with httpx.Client(follow_redirects=True, timeout=4.0) as client:
            resp = client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            return False
        data = resp.content
        if len(data) > _MAX_BYTES:
            return False
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            if img.width > _MAX_WIDTH:
                ratio = _MAX_WIDTH / img.width
                img = img.resize((_MAX_WIDTH, max(1, int(img.height * ratio))))
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            img.save(tmp, "JPEG", quality=_JPEG_QUALITY, optimize=True)
            tmp.rename(path)
        return True
    except Exception as e:  # noqa: BLE001 - image fetch must never break mint.
        log.info("game card image cache failed for %s: %s", card_id, e)
        return False
