"""Lightweight country-name → ISO-3166 alpha-2 map for ask intent detection.

A starter set of the most-asked-about countries (general-public phrasing).
Used to collapse "what's happening in <country>" onto a stable cache key
(intent:country:<cc>) so those questions hit the cache and line up with the
pre-baked set. Not exhaustive — unmatched questions fall through to semantic
search, which is the correct behavior.
"""

from __future__ import annotations

# Lowercased name / common alias → ISO2.
COUNTRY_ALIASES: dict[str, str] = {
    "united states": "US", "usa": "US", "the us": "US", "america": "US", "u.s.": "US",
    "united kingdom": "GB", "uk": "GB", "britain": "GB", "england": "GB",
    "russia": "RU", "ukraine": "UA", "china": "CN", "taiwan": "TW",
    "japan": "JP", "south korea": "KR", "korea": "KR", "north korea": "KP",
    "india": "IN", "pakistan": "PK", "iran": "IR", "iraq": "IQ", "israel": "IL",
    "palestine": "PS", "gaza": "PS", "lebanon": "LB", "syria": "SY",
    "saudi arabia": "SA", "turkey": "TR", "egypt": "EG", "yemen": "YE",
    "france": "FR", "germany": "DE", "italy": "IT", "spain": "ES",
    "poland": "PL", "netherlands": "NL", "sweden": "SE", "greece": "GR",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "venezuela": "VE", "colombia": "CO", "australia": "AU", "new zealand": "NZ",
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "ethiopia": "ET",
    "sudan": "SD", "afghanistan": "AF", "indonesia": "ID", "philippines": "PH",
    "vietnam": "VN", "thailand": "TH", "myanmar": "MM",
}


# Canonical ISO2 → display name, for rendering degraded answers (which must
# never show a bare 2-letter code to users). Unknown codes return None so the
# caller omits the location rather than printing "IS".
CODE_TO_NAME: dict[str, str] = {
    "US": "the United States", "GB": "the United Kingdom", "RU": "Russia",
    "UA": "Ukraine", "CN": "China", "TW": "Taiwan", "JP": "Japan",
    "KR": "South Korea", "KP": "North Korea", "IN": "India", "PK": "Pakistan",
    "IR": "Iran", "IQ": "Iraq", "IL": "Israel", "PS": "Palestine",
    "LB": "Lebanon", "SY": "Syria", "SA": "Saudi Arabia", "TR": "Turkey",
    "EG": "Egypt", "YE": "Yemen", "FR": "France", "DE": "Germany",
    "IT": "Italy", "ES": "Spain", "PL": "Poland", "NL": "the Netherlands",
    "SE": "Sweden", "GR": "Greece", "CA": "Canada", "MX": "Mexico",
    "BR": "Brazil", "AR": "Argentina", "VE": "Venezuela", "CO": "Colombia",
    "AU": "Australia", "NZ": "New Zealand", "ZA": "South Africa",
    "NG": "Nigeria", "KE": "Kenya", "ET": "Ethiopia", "SD": "Sudan",
    "AF": "Afghanistan", "ID": "Indonesia", "PH": "the Philippines",
    "VN": "Vietnam", "TH": "Thailand", "MM": "Myanmar", "IS": "Iceland",
}


def country_name(code: str | None) -> str | None:
    """ISO2 → display name, or None if unknown (so callers omit rather than
    print a bare code)."""
    if not code:
        return None
    return CODE_TO_NAME.get(code.upper())


def detect_country(text: str) -> str | None:
    """Return an ISO2 code if the text names a known country, else None.
    Longest alias first so 'south korea' beats 'korea'."""
    t = text.lower()
    for alias in sorted(COUNTRY_ALIASES, key=len, reverse=True):
        if alias in t:
            return COUNTRY_ALIASES[alias]
    return None


# A small default set of "top countries" to pre-bake when no live ranking is
# available — high-salience, general-public-relevant.
DEFAULT_TOP_COUNTRIES: list[str] = ["US", "UA", "RU", "IL", "CN", "GB"]
