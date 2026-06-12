"""Shared junk-title detection + its breaking-gate veto."""

from __future__ import annotations

import unittest

from worldview_api.scoring import is_breaking
from worldview_api.titles import is_generic_title, normalize_title


class GenericTitleTests(unittest.TestCase):
    def test_bare_section_names(self):
        self.assertTrue(is_generic_title("World"))
        self.assertTrue(is_generic_title("  Top Stories "))
        self.assertTrue(is_generic_title("MIDDLE EAST"))
        self.assertTrue(is_generic_title("World | News"))  # separators collapse

    def test_real_headlines_pass(self):
        self.assertFalse(is_generic_title("US launches fresh strikes on Iran"))
        self.assertFalse(is_generic_title("World leaders react to ceasefire"))
        self.assertFalse(is_generic_title("Flood Warning issued for Iowa"))

    def test_empty_is_junk(self):
        self.assertTrue(is_generic_title(None))
        self.assertTrue(is_generic_title("   "))

    def test_normalizer_collapses_separators(self):
        self.assertEqual(normalize_title("World — News | Live"), "world news live")


class BreakingVetoTests(unittest.TestCase):
    def test_junk_title_vetoes_event_count_branch(self):
        # The 119-member "World" magnet cluster must never be breaking.
        self.assertFalse(is_breaking(119, 0.95, "World"))

    def test_junk_title_vetoes_importance_branch(self):
        self.assertFalse(is_breaking(1, 0.99, "Top Stories"))

    def test_real_titles_keep_existing_behavior(self):
        self.assertTrue(is_breaking(12, 0.5, "US launches strikes on Iran"))
        self.assertTrue(is_breaking(2, 0.95, "US launches strikes on Iran"))
        self.assertFalse(is_breaking(2, 0.5, "US launches strikes on Iran"))

    def test_no_title_keeps_existing_behavior(self):
        self.assertTrue(is_breaking(12, 0.5))
        self.assertFalse(is_breaking(2, 0.5))


if __name__ == "__main__":
    unittest.main()
