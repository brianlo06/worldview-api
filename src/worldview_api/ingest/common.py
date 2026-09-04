"""Helpers shared by the GDELT events and GKG ingestion workers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..config import settings

GDELT_LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
# The translingual feed: the same three files, built from GDELT's machine
# translation of 65 non-English languages. The English feed runs ~53% US;
# this one runs ~5%, so it is what makes the globe actually global.
GDELT_TRANSLATION_LASTUPDATE_URL = (
    "https://data.gdeltproject.org/gdeltv2/lastupdate-translation.txt"
)


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


_STAMP_RE = re.compile(r"/(\d{14})\.")


def first_available(url: str, max_back: int = 6) -> str | None:
    """Return `url`, or the newest earlier 15-minute slot that is published.

    The translingual GKG is written up to ~45 minutes behind the timestamp its
    own lastupdate-translation.txt advertises, so the named file frequently
    404s. Walk back in 15-minute steps until one exists rather than skipping
    the cycle. Returns None when nothing in the window is published yet.
    """
    m = _STAMP_RE.search(url)
    if not m:
        return url
    try:
        stamp = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return url
    for back in range(max_back + 1):
        slot = (stamp - timedelta(minutes=15 * back)).strftime("%Y%m%d%H%M%S")
        candidate = _STAMP_RE.sub(f"/{slot}.", url, count=1)
        try:
            resp = httpx.head(
                candidate, timeout=20, headers=http_headers(), follow_redirects=True
            )
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            return candidate
    return None
