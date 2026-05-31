"""Tests for the GKG importance heuristic.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_importance`
"""

from __future__ import annotations

import unittest

from worldview_api.ingest.gdelt_gkg import importance_from_row


def _row(
    avg_tone: float = 0.0,
    persons: str = "",
    orgs: str = "",
) -> dict[str, str]:
    return {
        "V15TONE": f"{avg_tone},0,0,0,0,0,0",
        "V2ENHANCEDPERSONS": persons,
        "V2ENHANCEDORGANIZATIONS": orgs,
    }


def _entities(count: int) -> str:
    return ";".join(f"Entity_{i},100" for i in range(count))


class ImportanceFromRowTests(unittest.TestCase):
    def test_sparse_article_scores_low(self):
        # No tone, almost no themes/locations/entities — only the base term.
        row = _row(avg_tone=0.0)
        score = importance_from_row(row, theme_count=0, loc_count=0)
        # base 0.15 + ~0 from all terms
        self.assertLess(score, 0.25)

    def test_tonally_extreme_alone_capped(self):
        # Very negative article but no themes/locations/entities.
        # Tone alone (cap 0.25) on top of base 0.15 cannot exceed 0.40, well
        # under the spec ceiling of 0.55 for a tone-only article.
        row = _row(avg_tone=-12.0)
        score = importance_from_row(row, theme_count=0, loc_count=0)
        self.assertLessEqual(score, 0.55)

    def test_multi_signal_rich_article_high(self):
        # Tonally loaded, theme-rich, location-rich, entity-rich.
        # All four caps should engage and stack on the base.
        row = _row(
            avg_tone=-7.0,
            persons=_entities(50),
            orgs=_entities(50),
        )
        score = importance_from_row(row, theme_count=80, loc_count=30)
        self.assertGreaterEqual(score, 0.75)
        self.assertLessEqual(score, 1.0)

    def test_typical_article_mid_scale(self):
        # A moderately-toned article with average GKG theme/loc/entity counts
        # should land mid-scale (the whole point of the rescale).
        row = _row(
            avg_tone=-4.0,
            persons=_entities(8),
            orgs=_entities(5),
        )
        score = importance_from_row(row, theme_count=15, loc_count=5)
        self.assertGreaterEqual(score, 0.35)
        self.assertLessEqual(score, 0.75)

    def test_output_clamped_to_unit_interval(self):
        # Even with absurd inputs, output stays in [0, 1].
        row = _row(avg_tone=-99.0, persons=_entities(500), orgs=_entities(500))
        score = importance_from_row(row, theme_count=999, loc_count=999)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
