"""Tests for the NWS severity gate.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_weather_severity`
"""

from __future__ import annotations

import unittest

from worldview_api.ingest.weather import SEVERITY_IMPORTANCE, SKIPPED_SEVERITIES


class SeverityScaleTests(unittest.TestCase):
    def test_minor_and_unknown_are_dropped_not_scored(self) -> None:
        # These are ~60% of the feed and are not world news.
        self.assertEqual(SKIPPED_SEVERITIES, frozenset({"Minor", "Unknown"}))
        for severity in SKIPPED_SEVERITIES:
            self.assertNotIn(severity, SEVERITY_IMPORTANCE)

    def test_only_moderate_and_above_are_scored(self) -> None:
        self.assertEqual(
            set(SEVERITY_IMPORTANCE), {"Extreme", "Severe", "Moderate"}
        )

    def test_importance_decreases_with_severity(self) -> None:
        self.assertGreater(
            SEVERITY_IMPORTANCE["Extreme"], SEVERITY_IMPORTANCE["Severe"]
        )
        self.assertGreater(
            SEVERITY_IMPORTANCE["Severe"], SEVERITY_IMPORTANCE["Moderate"]
        )

    def test_moderate_sits_below_the_frontends_importance_floor(self) -> None:
        # Globe.tsx requests min_importance=0.3; Moderate should not outrank
        # the median GKG article (~0.55) the way the old 0.60 scale did.
        self.assertLess(SEVERITY_IMPORTANCE["Moderate"], 0.55)

    def test_extreme_stays_above_the_gkg_ceiling(self) -> None:
        # GKG importance saturates at 0.90 and only outliers exceed 0.80;
        # a genuine Extreme alert should still surface as breaking news.
        self.assertGreaterEqual(SEVERITY_IMPORTANCE["Extreme"], 0.80)


if __name__ == "__main__":
    unittest.main()
