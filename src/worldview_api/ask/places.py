"""Country-name/abbreviation → GDELT FIPS 10-4 code map for ask intent.

A starter set of the most-asked-about countries (general-public phrasing,
including abbreviations like "US" and "UK"). Used to collapse "what's
happening in <country>" onto a stable cache key (intent:country:<cc>) so
those questions hit the cache and line up with the pre-baked set. Not
exhaustive — unmatched questions fall through to semantic search, which is
the correct behavior.

The codes are FIPS, NOT ISO: events/clusters store GDELT's FIPS 10-4 codes
(Russia=RS, China=CH, Germany=GM, UK=UK...), and the country intent queries
clusters.primary_country directly. The original ISO values silently missed
every collision country. Every code below is validated against
worldview_api.regions (the FIPS-aware map) by tests/test_places.py.
"""

from __future__ import annotations

import re

# Lowercased name / common alias / abbreviation → FIPS code. Matching is
# word-bounded (see detect_country), so short aliases like "us" are safe.
COUNTRY_ALIASES: dict[str, str] = {
    "united states": "US", "usa": "US", "u.s.a": "US", "u.s": "US",
    "us": "US", "the states": "US", "america": "US",
    "united kingdom": "UK", "uk": "UK", "u.k": "UK", "britain": "UK",
    "great britain": "UK", "england": "UK",
    "russia": "RS", "ukraine": "UP", "china": "CH", "taiwan": "TW",
    "japan": "JA", "south korea": "KS", "korea": "KS", "north korea": "KN",
    "india": "IN", "pakistan": "PK", "iran": "IR", "iraq": "IZ",
    "israel": "IS", "palestine": "WE", "west bank": "WE", "gaza": "GZ",
    "lebanon": "LE", "syria": "SY", "saudi arabia": "SA", "turkey": "TU",
    "egypt": "EG", "yemen": "YM",
    "united arab emirates": "AE", "uae": "AE", "u.a.e": "AE",
    "france": "FR", "germany": "GM", "italy": "IT", "spain": "SP",
    "poland": "PL", "netherlands": "NL", "sweden": "SW", "greece": "GR",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "venezuela": "VE", "colombia": "CO", "australia": "AS",
    "new zealand": "NZ", "south africa": "SF", "nigeria": "NI",
    "kenya": "KE", "ethiopia": "ET", "sudan": "SU", "afghanistan": "AF",
    "indonesia": "ID", "philippines": "RP", "vietnam": "VM",
    "thailand": "TH", "myanmar": "BM",
}

# Aliases sorted longest-first so "south korea" beats "korea" and
# "ukraine" beats "uk", each compiled with letter-boundary guards so "us"
# can't match inside "russia" (plain \b misbehaves around the dots in
# "u.s.", hence the lookarounds).
_ALIAS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"(?<![a-z]){re.escape(alias)}(?![a-z])"), code)
    for alias, code in sorted(
        COUNTRY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True
    )
]


def detect_country(text: str) -> str | None:
    """Return a FIPS code if the text names a known country (full name or
    abbreviation), else None."""
    t = text.lower()
    for pattern, code in _ALIAS_PATTERNS:
        if pattern.search(t):
            return code
    return None


# A small default set of "top countries" to pre-bake when no live ranking is
# available — high-salience, general-public-relevant. FIPS codes.
DEFAULT_TOP_COUNTRIES: list[str] = ["US", "UP", "RS", "IS", "CH", "UK"]
