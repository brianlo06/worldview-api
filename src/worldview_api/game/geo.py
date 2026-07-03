"""Country code → continent for collection badges.

Codes mirror regions.py's hybrid FIPS/ISO table (FIPS meaning wins on
collisions, so ES=El Salvador, MA=Madagascar, BY=Burundi, AS=Australia).
Keep in sync when regions.py grows."""

from __future__ import annotations

_CONTINENTS: dict[str, set[str]] = {
    "north-america": {
        "US", "CA", "MX", "CU", "HA", "HT", "DR", "JM", "GT", "HO", "HN",
        "NU", "PM", "CS", "CR", "BB", "TD", "TT", "BF", "BS", "GJ", "ES",
    },
    "south-america": {
        "BR", "AR", "CI", "CL", "CO", "PE", "VE", "EC", "BL", "UY", "PA",
        "PY", "GY", "SR",
    },
    "europe": {
        "UK", "GB", "EI", "IE", "FR", "GM", "DE", "IT", "SP", "PO", "PT",
        "NL", "BE", "SZ", "AU", "AT", "SW", "SE", "NO", "DA", "DK", "FI",
        "IC", "PL", "EZ", "CZ", "HU", "RO", "GR", "RB", "HR", "AL", "MJ",
        "MK", "SI", "LO", "SK", "UP", "UA", "BO", "RS", "RU", "TU", "TR",
        "LU", "MT", "AN", "GI", "MD", "BU",
    },
    "africa": {
        "EG", "LY", "TS", "AG", "DZ", "MO", "MA", "SU", "SD", "SO", "ET",
        "KE", "UG", "TZ", "RW", "BY", "NI", "NG", "NE", "GH", "IV", "ML",
        "SG", "CM", "CG", "CF", "CD", "SF", "ZA", "ZI", "ZW", "AO", "MZ",
        "WA", "TO", "EK", "GA", "GV", "PU",
    },
    "asia": {
        "IS", "IL", "LE", "LB", "SY", "JO", "IZ", "IQ", "IR", "SA", "AE",
        "TC", "QA", "KU", "KW", "YM", "YE", "BA", "BH", "MU", "OM", "IN",
        "PK", "BG", "BD", "CE", "LK", "NP", "AF", "BT", "MV", "JA", "JP",
        "KS", "KR", "KN", "KP", "CH", "CN", "TW", "HK", "MC", "VM", "VN",
        "TH", "LA", "CB", "KH", "BM", "MM", "MY", "ID", "RP", "PH", "SN",
        "MG", "MN", "KZ", "UZ", "KG", "TI", "TJ", "TX", "TM", "AJ", "AZ",
        "AM", "GG", "GE",
    },
    "oceania": {"AS", "NZ", "PP", "PG", "FJ", "NH", "VU", "WS"},
}

CONTINENT_OF: dict[str, str] = {
    code: continent for continent, codes in _CONTINENTS.items() for code in codes
}


def continent_of(country_code: str | None) -> str | None:
    if not country_code:
        return None
    return CONTINENT_OF.get(country_code.strip().upper())
