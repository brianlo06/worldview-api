"""Shared junk-title detection.

A scraped section page ("World", "Top Stories", "Middle East") is not a
headline. The GKG ingest refuses such events outright, but events from
before that gate existed are still in the table — so the representative
picker and the breaking gate apply the same test defensively. One list,
one normalizer, used everywhere (plus a SQL-side mirror for the picker).
"""

from __future__ import annotations

import re

GENERIC_SECTION_TITLES: frozenset[str] = frozenset({
    "world", "world news", "news", "home", "homepage", "latest news",
    "breaking news", "top stories", "opinion", "editorial", "sports", "sport",
    "business", "politics", "entertainment", "lifestyle", "culture", "health",
    "technology", "tech", "science", "video", "videos", "photos", "live",
    "local news", "national", "international", "africa", "americas", "asia",
    "europe", "middle east", "uk", "us",
})

# Separator runs ("World | Arab News" → "world arab news") — mirrored in SQL
# by GENERIC_TITLE_NORMALIZE_SQL_RE below.
_SEPARATORS = re.compile(r"[\s|·\-–—:]+")

# The same separator class for postgres regexp_replace(..., 'g').
GENERIC_TITLE_NORMALIZE_SQL_RE = r"[\s|·\-–—:]+"


def normalize_title(title: str) -> str:
    return _SEPARATORS.sub(" ", title.strip().lower()).strip()


def is_generic_title(title: str | None) -> bool:
    """True when the title is a bare site-section name (or blank), not a
    headline."""
    if not title:
        return True
    t = normalize_title(title)
    return not t or t in GENERIC_SECTION_TITLES
