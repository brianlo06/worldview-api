"""Tests for the shared is_breaking helper.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_scoring`
"""

from __future__ import annotations

import unittest

from worldview_api.scoring import is_breaking, tier_where_clause


class IsBreakingTests(unittest.TestCase):
    def test_high_event_count_low_importance_is_breaking(self):
        # Broad coverage alone is enough.
        self.assertTrue(is_breaking(event_count=25, importance=0.30))

    def test_low_event_count_high_importance_is_breaking(self):
        # Top-of-distribution importance alone is enough.
        self.assertTrue(is_breaking(event_count=1, importance=0.95))

    def test_mid_event_count_mid_importance_is_not_breaking(self):
        # The typical case must NOT trip the flag — this is the bug we're fixing.
        self.assertFalse(is_breaking(event_count=3, importance=0.55))

    def test_null_importance_low_event_count_is_not_breaking(self):
        self.assertFalse(is_breaking(event_count=2, importance=None))

    def test_null_importance_high_event_count_is_breaking(self):
        # event_count branch is independent of importance.
        self.assertTrue(is_breaking(event_count=15, importance=None))

    def test_event_count_threshold_boundary(self):
        self.assertFalse(is_breaking(event_count=9, importance=0.50))
        self.assertTrue(is_breaking(event_count=10, importance=0.50))

    def test_importance_threshold_boundary(self):
        self.assertFalse(is_breaking(event_count=1, importance=0.91))
        self.assertTrue(is_breaking(event_count=1, importance=0.92))


class TierWhereClauseTests(unittest.TestCase):
    """Pin the predicate text + params so accidental drift is caught.

    These constants are shared with the frontend's `passesTier`; if either side
    moves, the other must follow.
    """

    def test_all_is_empty(self):
        self.assertEqual(tier_where_clause("all"), ("", []))

    def test_notable_is_importance_threshold(self):
        sql, params = tier_where_clause("notable")
        self.assertEqual(sql, "coalesce(c.importance_score, 0) >= %s")
        self.assertEqual(params, [0.65])

    def test_major_is_importance_threshold(self):
        sql, params = tier_where_clause("major")
        self.assertEqual(sql, "coalesce(c.importance_score, 0) >= %s")
        self.assertEqual(params, [0.68])

    def test_top_is_importance_threshold(self):
        sql, params = tier_where_clause("top")
        self.assertEqual(sql, "coalesce(c.importance_score, 0) >= %s")
        self.assertEqual(params, [0.75])

    def test_unknown_tier_raises(self):
        with self.assertRaises(ValueError):
            tier_where_clause("foo")


if __name__ == "__main__":
    unittest.main()
