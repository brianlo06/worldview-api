"""Country intent detection (ask/places.py) — FIPS codes + abbreviations."""

from __future__ import annotations

import unittest

from worldview_api.ask.places import (
    COUNTRY_ALIASES,
    DEFAULT_TOP_COUNTRIES,
    detect_country,
)
from worldview_api.regions import region_name


class AliasCodeTests(unittest.TestCase):
    def test_every_alias_code_is_a_known_fips_region(self):
        # Gaza/West Bank are real GDELT FIPS codes present in the data but
        # absent from the (frontend-derived) display map — exempt.
        exempt = {"GZ", "WE"}
        for alias, code in COUNTRY_ALIASES.items():
            if code in exempt:
                continue
            self.assertIsNotNone(
                region_name(code), f"alias {alias!r} -> unknown code {code!r}"
            )

    def test_collision_countries_use_fips_not_iso(self):
        # The original ISO values silently missed these in the FIPS data.
        self.assertEqual(COUNTRY_ALIASES["russia"], "RS")
        self.assertEqual(COUNTRY_ALIASES["china"], "CH")
        self.assertEqual(COUNTRY_ALIASES["japan"], "JA")
        self.assertEqual(COUNTRY_ALIASES["germany"], "GM")
        self.assertEqual(COUNTRY_ALIASES["ukraine"], "UP")
        self.assertEqual(COUNTRY_ALIASES["iraq"], "IZ")
        self.assertEqual(COUNTRY_ALIASES["israel"], "IS")
        self.assertEqual(COUNTRY_ALIASES["united kingdom"], "UK")

    def test_default_top_countries_are_known_fips(self):
        for code in DEFAULT_TOP_COUNTRIES:
            self.assertIsNotNone(region_name(code), code)


class DetectCountryTests(unittest.TestCase):
    def test_full_names(self):
        self.assertEqual(detect_country("what's happening in Russia"), "RS")
        self.assertEqual(detect_country("news from south korea today"), "KS")
        self.assertEqual(detect_country("What's happening in Germany?"), "GM")

    def test_abbreviations(self):
        self.assertEqual(detect_country("what's happening in the US"), "US")
        self.assertEqual(detect_country("what's happening in US?"), "US")
        self.assertEqual(detect_country("latest from the u.s. today"), "US")
        self.assertEqual(detect_country("usa headlines"), "US")
        self.assertEqual(detect_country("what's happening in the UK"), "UK")
        self.assertEqual(detect_country("u.k. politics"), "UK")
        self.assertEqual(detect_country("news in the UAE"), "AE")

    def test_short_aliases_are_word_bounded(self):
        # "us" must not fire inside "russia", "uk" not inside "ukraine".
        self.assertEqual(detect_country("what's happening in russia"), "RS")
        self.assertEqual(detect_country("what's happening in ukraine"), "UP")
        # ...and not inside arbitrary words either.
        self.assertIsNone(detect_country("dust storms and museums"))

    def test_longest_alias_wins(self):
        self.assertEqual(detect_country("south korea vs korea"), "KS")
        self.assertEqual(detect_country("the west bank today"), "WE")

    def test_no_match_returns_none(self):
        self.assertIsNone(detect_country("what's happening with the economy"))


if __name__ == "__main__":
    unittest.main()
