"""Anomaly synopsis generation (no DB / no live LLM)."""

from __future__ import annotations

import unittest
from unittest import mock

from worldview_api.analyze import synopsis as S
from worldview_api.config import settings
from worldview_api.regions import region_name


class RegionNameTests(unittest.TestCase):
    """The map must resolve GDELT FIPS collisions to the FIPS meaning."""

    def test_fips_collisions(self):
        self.assertEqual(region_name("RS"), "Russia")
        self.assertEqual(region_name("IS"), "Israel")
        self.assertEqual(region_name("CH"), "China")
        self.assertEqual(region_name("JA"), "Japan")
        self.assertEqual(region_name("MO"), "Morocco")

    def test_unknown_returns_none(self):
        self.assertIsNone(region_name("ZZ"))
        self.assertIsNone(region_name(None))


class SynopsisTests(unittest.TestCase):
    def test_template_with_titles(self):
        out = S._template_synopsis("Russia", 5.3, ["EU sanctions package expanded"])
        self.assertIn("Russia", out)
        self.assertIn("5.3×", out)
        self.assertIn("EU sanctions package expanded", out)

    def test_template_without_titles(self):
        out = S._template_synopsis("Japan", 3.0, [])
        self.assertIn("Japan", out)
        self.assertTrue(out.endswith("."))

    def test_no_api_key_degrades_to_template(self):
        with mock.patch.object(settings, "llm_api_key", ""):
            out = S.generate_synopsis("RS", 5.3, ["Some headline"])
        self.assertIn("Russia", out)
        self.assertIn("Some headline", out)

    def test_budget_exhausted_degrades_to_template(self):
        with mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(S.budget, "try_acquire", return_value=False):
            out = S.generate_synopsis("JA", 4.0, ["Tech stocks slide"])
        self.assertIn("Japan", out)

    def test_llm_output_used_and_clamped(self):
        long_line = "JARVIS line " * 40
        fake_resp = mock.Mock()
        fake_resp.choices = [mock.Mock(message=mock.Mock(content=long_line))]
        fake_client = mock.Mock()
        fake_client.chat.completions.create.return_value = fake_resp
        with mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(S.budget, "try_acquire", return_value=True), \
             mock.patch.object(S, "get_client", return_value=fake_client):
            out = S.generate_synopsis("RS", 5.0, ["headline"])
        self.assertLessEqual(len(out), S.MAX_SYNOPSIS_CHARS + 1)
        self.assertTrue(out.startswith("JARVIS line"))


if __name__ == "__main__":
    unittest.main()
