"""Helpers shared by the GDELT events and GKG ingestion workers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import httpx

from ..config import settings

GDELT_LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"


def http_headers() -> dict[str, str]:
    return {"User-Agent": settings.gdelt_user_agent}


def gdelt_get(url: str, timeout: float) -> httpx.Response:
    """GET a GDELT URL, following redirects, and raise on a bad status.

    Every GDELT fetch must go through here. data.gdeltproject.org 301s
    plain HTTP to HTTPS, and the CSV zip URLs listed inside lastupdate.txt
    are still published as http:// regardless of how we fetched the index.
    httpx does not follow redirects by default and its raise_for_status()
    treats a 3xx as an error, so a bare httpx.get() here fails on the very
    first call and takes the whole GDELT pipeline down with it.
    """
    resp = httpx.get(
        url, timeout=timeout, headers=http_headers(), follow_redirects=True
    )
    resp.raise_for_status()
    return resp


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
