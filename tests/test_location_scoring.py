"""Tests for the GKG location scorer.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_location_scoring`
"""

from __future__ import annotations

import unittest

from worldview_api.ingest.gdelt_gkg import (
    parse_locations,
    pick_best_location,
    score_locations,
    type_to_precision,
)


def _v2(*items: tuple[int, str, str | None, float, float, int | None]) -> str:
    """Build a V2ENHANCEDLOCATIONS string from typed tuples for tests."""
    parts = []
    for type_, name, cc, lat, lon, offset in items:
        # Format: type#name#cc#adm1#adm2#lat#lon#featureid[#offset]
        base = f"{type_}#{name}#{cc or ''}##{''}#{lat}#{lon}#FEAT"
        if offset is not None:
            base = f"{base}#{offset}"
        parts.append(base)
    return ";".join(parts)


class ParseLocationsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_locations(""), [])
        self.assertEqual(parse_locations(None), [])

    def test_captures_offset(self):
        s = _v2((4, "Beijing", "CH", 39.9, 116.4, 120))
        result = parse_locations(s)
        self.assertEqual(len(result), 1)
        _t, _n, _cc, _lat, _lon, offset = result[0]
        self.assertEqual(offset, 120)

    def test_offset_absent(self):
        # Some GKG rows truncate at featureid; we should still parse them.
        s = "4#Beijing#CH##0#39.9#116.4#FEAT"
        result = parse_locations(s)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0][5])

    def test_drops_zero_zero(self):
        s = _v2((1, "Nowhere", "ZZ", 0.0, 0.0, 0))
        self.assertEqual(parse_locations(s), [])

    def test_one_mention_per_tuple(self):
        # Beijing mentioned 3 times = 3 tuples
        s = _v2(
            (4, "Beijing", "CH", 39.9, 116.4, 50),
            (4, "Beijing", "CH", 39.9, 116.4, 320),
            (4, "Beijing", "CH", 39.9, 116.4, 850),
        )
        self.assertEqual(len(parse_locations(s)), 3)


class ScoreLocationsTests(unittest.TestCase):
    def test_beijing_vs_china_vs_taiwan(self):
        """Article topically about Beijing; mention counts: Beijing×3, China×4, Taiwan×2."""
        locs = parse_locations(
            _v2(
                (4, "Beijing", "CH", 39.9, 116.4, 80),
                (4, "Beijing", "CH", 39.9, 116.4, 400),
                (4, "Beijing", "CH", 39.9, 116.4, 1100),
                (1, "China", "CH", 35.0, 105.0, 50),
                (1, "China", "CH", 35.0, 105.0, 200),
                (1, "China", "CH", 35.0, 105.0, 700),
                (1, "China", "CH", 35.0, 105.0, 1400),
                (1, "Taiwan", "TW", 24.0, 121.0, 600),
                (1, "Taiwan", "TW", 24.0, 121.0, 1200),
            )
        )
        winner = pick_best_location(locs)
        self.assertIsNotNone(winner)
        self.assertEqual(winner[1], "Beijing")
        # And type 4 → city precision
        self.assertEqual(type_to_precision(winner[0]), "city")

    def test_falls_church_does_not_beat_astana(self):
        """The actual Kazakhstan/Türkiye trade article shape."""
        locs = parse_locations(
            _v2(
                (4, "Astana", "KZ", 51.16, 71.47, 90),
                (4, "Astana", "KZ", 51.16, 71.47, 400),
                (4, "Astana", "KZ", 51.16, 71.47, 880),
                (4, "Astana", "KZ", 51.16, 71.47, 1500),
                (4, "Falls Church", "US", 38.88, -77.17, 2200),
                (1, "Turkey", "TU", 39.0, 35.0, 60),
                (1, "Turkey", "TU", 39.0, 35.0, 300),
                (1, "Turkey", "TU", 39.0, 35.0, 1000),
            )
        )
        winner = pick_best_location(locs)
        self.assertEqual(winner[1], "Astana")

    def test_country_wins_when_no_city_competes(self):
        """Article about 'US inflation' with US mentioned 8× and Washington 1×.

        Country score: 8 * 0.3 = 2.4
        City score: 1 * 1.0 = 1.0
        Country wins → frontend will mark it as country-precision.
        """
        locs = parse_locations(
            _v2(
                *[(1, "United States", "US", 39.5, -98.35, 100 + i * 200) for i in range(8)],
                (4, "Washington", "US", 38.9, -77.04, 2200),
            )
        )
        winner = pick_best_location(locs)
        self.assertEqual(winner[1], "United States")
        self.assertEqual(type_to_precision(winner[0]), "country")

    def test_earliest_offset_breaks_ties(self):
        """Two locations with identical mention count and weight; earliest wins."""
        locs = parse_locations(
            _v2(
                (4, "Paris", "FR", 48.85, 2.35, 5000),
                (4, "Paris", "FR", 48.85, 2.35, 5200),
                (4, "London", "UK", 51.5, -0.13, 120),
                (4, "London", "UK", 51.5, -0.13, 800),
            )
        )
        winner = pick_best_location(locs)
        self.assertEqual(winner[1], "London")

    def test_empty(self):
        self.assertIsNone(pick_best_location([]))
        self.assertEqual(score_locations([]), [])


class TypeToPrecisionTests(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(type_to_precision(1), "country")
        self.assertEqual(type_to_precision(2), "state")
        self.assertEqual(type_to_precision(3), "city")
        self.assertEqual(type_to_precision(4), "city")
        self.assertEqual(type_to_precision(5), "state")
        self.assertEqual(type_to_precision(99), "country")  # safe fallback


if __name__ == "__main__":
    unittest.main()
