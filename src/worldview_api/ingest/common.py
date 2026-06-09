"""Helpers shared by the GDELT events and GKG ingestion workers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..config import settings

GDELT_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"


def http_headers() -> dict[str, str]:
    return {"User-Agent": settings.gdelt_user_agent}


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def parse_gdelt_timestamp(s: str | None) -> datetime:
    """Parse a GDELT YYYYMMDDHHMMSS (or bare YYYYMMDD) stamp as UTC.

    Falls back to now() on anything unparseable — both ingest paths prefer a
    slightly wrong timestamp over dropping the row.
    """
    s = (s or "").strip()
    try:
        if len(s) == 14:
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        if len(s) == 8:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return datetime.now(tz=timezone.utc)
