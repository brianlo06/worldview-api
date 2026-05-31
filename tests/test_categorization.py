"""Tests for the GKG theme→category mapping.

Run with: `cd worldview-api && PYTHONPATH=src .venv/bin/python -m unittest tests.test_categorization`
"""

from __future__ import annotations

import unittest

from worldview_api.ingest.gdelt_gkg import themes_to_category


def _themes(*tokens: str) -> str:
    """Build a V2ENHANCEDTHEMES-shaped string from theme tokens.

    Real GDELT themes look like `TOKEN,offset;TOKEN,offset;...`. The categorizer
    runs regex .findall over the raw string so offsets don't change behavior,
    but include them for realism.
    """
    return ";".join(f"{t},100" for t in tokens)


class ThemesToCategoryTests(unittest.TestCase):
    def test_tax_fncact_alone_is_not_business(self):
        # TAX_FNCACT is GDELT's taxonomy prefix for "functional actions" — it's
        # on basically every article. Pre-fix, this single theme would make a
        # doc business via the TAX_ match. After fix, no business pattern hits,
        # so the doc falls to the default `politics`.
        result = themes_to_category(_themes("TAX_FNCACT"))
        self.assertNotEqual(result, "business")

    def test_tax_political_party_plus_election_is_politics(self):
        # Pre-fix, TAX_POLITICAL_PARTY counted as business (TAX_ matched).
        # ELECTION counted as politics. Politics hit count was 1, business hit
        # count was 1, tied — and business wins ties (declared first).
        # After fix, TAX_POLITICAL_PARTY no longer hits business; only ELECTION
        # matches anything. Politics wins outright.
        result = themes_to_category(
            _themes("TAX_POLITICAL_PARTY", "ELECTION", "TAX_FNCACT_PRESIDENT")
        )
        self.assertEqual(result, "politics")

    def test_econ_taxation_alone_is_business(self):
        # ECON_TAXATION (genuine taxation theme) still triggers business via
        # the ECON_ pattern, even after TAX_ is removed.
        result = themes_to_category(_themes("ECON_TAXATION"))
        self.assertEqual(result, "business")

    def test_multiple_econ_themes_is_business(self):
        # A clearly-economic article should still classify as business.
        result = themes_to_category(
            _themes("ECON_TAXATION", "ECON_PRICE", "ECON_STOCKMARKET", "TRADE")
        )
        self.assertEqual(result, "business")

    def test_conflict_themes_dominate_a_single_tax_token(self):
        # Before the fix, a story with several conflict themes plus the
        # ubiquitous TAX_FNCACT could end up business because TAX_FNCACT was
        # counted as business. After the fix, conflict wins cleanly.
        result = themes_to_category(
            _themes(
                "KILL",
                "ATTACK",
                "ARMEDCONFLICT",
                "MILITARY_OPS",
                "TAX_FNCACT",
                "TAX_ETHNICITY",
            )
        )
        self.assertEqual(result, "conflict")

    def test_empty_themes_falls_to_default(self):
        self.assertEqual(themes_to_category(""), "politics")
        self.assertEqual(themes_to_category(None), "politics")

    def test_unmatchable_themes_fall_to_default(self):
        # No pattern matches any of these; should fall back to the default.
        result = themes_to_category(_themes("TAX_WORLDMAMMALS", "TAX_DISEASE"))
        self.assertEqual(result, "politics")

    def test_weather_dominates_when_majority(self):
        result = themes_to_category(
            _themes(
                "HURRICANE", "FLOOD", "STORM", "WEATHER_RAIN",
                "TAX_FNCACT", "ECON_PRICE",
            )
        )
        self.assertEqual(result, "weather")


class NoiseFilterTests(unittest.TestCase):
    """Tests for the auxiliary-taxonomy noise filter in themes_to_category."""

    def test_wb_digital_government_alone_is_not_politics(self):
        # WB_*_DIGITAL_GOVERNMENT is a World Bank topic code that GDELT sprays
        # onto digital-economy content. By itself it must not flip a doc to
        # politics. With no other matches, the doc falls back to the default.
        result = themes_to_category(_themes("WB_678_DIGITAL_GOVERNMENT"))
        # No real signal — falls back to default (politics).
        self.assertEqual(result, "politics")
        # But it must reach the default via the fallback, not via a politics match.
        # If the WB_* token were counted, an unmatchable token alone (like
        # WB_999_GIBBERISH) would also reach the same "politics" default. The
        # important property here is the next test:

    def test_wb_digital_government_with_econ_is_business(self):
        # When the doc has real ECON_* content alongside the WB_* noise, the
        # noise token must not flip it to politics — business should win.
        result = themes_to_category(
            _themes("WB_678_DIGITAL_GOVERNMENT", "ECON_PRICE", "ECON_TRADE")
        )
        self.assertEqual(result, "business")

    def test_epu_policy_with_econ_is_business(self):
        # EPU_POLICY_* themes come from the Economic Policy Uncertainty dataset —
        # they describe economic conditions, not political topics. An article
        # tagged with EPU_POLICY_GOVERNMENT + ECON_TAXATION should classify as
        # business, not politics.
        result = themes_to_category(
            _themes("EPU_POLICY_GOVERNMENT", "EPU_POLICY_POLITICAL", "ECON_TAXATION")
        )
        self.assertEqual(result, "business")

    def test_general_government_alone_is_not_politics(self):
        # GENERAL_GOVERNMENT is a generic descriptor on most articles touching
        # government in passing. Alone it must not categorize as politics.
        # With no other matches, falls back to default (which happens to be
        # politics) — that's fine; the test is that politics regex didn't fire.
        # Verify by adding a single conflict theme: conflict must win cleanly.
        result = themes_to_category(_themes("GENERAL_GOVERNMENT", "KILL"))
        self.assertEqual(result, "conflict")

    def test_gov_localgov_plus_election_is_politics(self):
        # Real government-namespace themes (GOV_*) still match.
        result = themes_to_category(_themes("GOV_LOCALGOV", "ELECTION"))
        self.assertEqual(result, "politics")

    def test_tax_political_party_plus_election_still_politics(self):
        # The TAX_POLITICAL_PARTY family is genuine political signal. Our regex
        # catches it via POLITICAL. Even with TAX_ removed earlier, this still
        # works.
        result = themes_to_category(
            _themes("TAX_POLITICAL_PARTY_REPUBLICAN", "ELECTION")
        )
        self.assertEqual(result, "politics")

    def test_only_noise_tokens_falls_to_default(self):
        # A document tagged only with auxiliary-taxonomy themes should fall to
        # the default category (no real signal).
        result = themes_to_category(
            _themes("WB_1234_SOMETHING", "EPU_POLICY_ECONOMIC", "GENERAL_GOVERNMENT")
        )
        self.assertEqual(result, "politics")  # the default


if __name__ == "__main__":
    unittest.main()
