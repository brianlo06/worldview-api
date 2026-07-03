"""Tests for the game spine's pure logic + identity hashing (no DB required).

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_game_logic`
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import date
from random import Random

from worldview_api.game import logic
from worldview_api.game.identity import hash_token, mint_token
from worldview_api.game.rates import load_config

WEIGHTS = {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1}
PITY = {"epic": 20, "legendary": 90}


class TokenTests(unittest.TestCase):
    def test_mint_returns_token_and_matching_hash(self):
        token, digest = mint_token()
        self.assertEqual(digest, hash_token(token))
        self.assertEqual(len(digest), 64)  # sha256 hex
        self.assertNotIn(token, digest)

    def test_tokens_are_unique(self):
        self.assertNotEqual(mint_token()[0], mint_token()[0])


class RollTests(unittest.TestCase):
    def test_boundaries_map_to_expected_tiers(self):
        # Cumulative: common <60, uncommon <85, rare <95, epic <99, legendary <100
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.0), "common")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.599), "common")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.60), "uncommon")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.849), "uncommon")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.85), "rare")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.95), "epic")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.99), "legendary")
        self.assertEqual(logic.roll_tier(WEIGHTS, 0.9999), "legendary")

    def test_distribution_sanity(self):
        rng = Random(42)
        n = 200_000
        counts = Counter(logic.roll_tier(WEIGHTS, rng.random()) for _ in range(n))
        self.assertAlmostEqual(counts["common"] / n, 0.60, delta=0.01)
        self.assertAlmostEqual(counts["uncommon"] / n, 0.25, delta=0.01)
        self.assertAlmostEqual(counts["rare"] / n, 0.10, delta=0.01)
        self.assertAlmostEqual(counts["epic"] / n, 0.04, delta=0.005)
        self.assertAlmostEqual(counts["legendary"] / n, 0.01, delta=0.003)

    def test_seed_fraction_roundtrip_is_deterministic(self):
        seed = "a3f09c2d11e4bb07"
        f1 = logic.seed_to_fraction(seed)
        f2 = logic.seed_to_fraction(seed)
        self.assertEqual(f1, f2)
        self.assertTrue(0.0 <= f1 < 1.0)
        self.assertEqual(
            logic.roll_tier(WEIGHTS, f1), logic.roll_tier(WEIGHTS, f2)
        )


class PityTests(unittest.TestCase):
    def test_twentieth_roll_without_epic_is_forced(self):
        # 19 rolls have passed without epic+: the 20th must be epic or better.
        tier, hit = logic.apply_pity("common", 19, 30, PITY, WEIGHTS, 0.0)
        self.assertIn(tier, ("epic", "legendary"))
        self.assertEqual(hit, "epic")

    def test_nineteenth_roll_is_not_forced(self):
        tier, hit = logic.apply_pity("common", 18, 30, PITY, WEIGHTS, 0.0)
        self.assertEqual(tier, "common")
        self.assertIsNone(hit)

    def test_legendary_pity_forces_legendary(self):
        tier, hit = logic.apply_pity("common", 0, 89, PITY, WEIGHTS, 0.0)
        self.assertEqual(tier, "legendary")
        self.assertEqual(hit, "legendary")

    def test_natural_epic_not_marked_as_pity(self):
        tier, hit = logic.apply_pity("epic", 19, 30, PITY, WEIGHTS, 0.0)
        self.assertEqual(tier, "epic")
        self.assertIsNone(hit)

    def test_counters_reset_on_hits(self):
        self.assertEqual(logic.next_pity("legendary", 15, 80), (0, 0))
        self.assertEqual(logic.next_pity("epic", 15, 80), (0, 81))
        self.assertEqual(logic.next_pity("rare", 15, 80), (16, 81))

    def test_pity_cycle_never_exceeds_thresholds(self):
        rng = Random(7)
        since_epic, since_legendary = 0, 0
        for _ in range(5000):
            natural = logic.roll_tier(WEIGHTS, rng.random())
            tier, _hit = logic.apply_pity(
                natural, since_epic, since_legendary, PITY, WEIGHTS, rng.random()
            )
            since_epic, since_legendary = logic.next_pity(tier, since_epic, since_legendary)
            self.assertLess(since_epic, PITY["epic"])
            self.assertLess(since_legendary, PITY["legendary"])


class TierAvailabilityTests(unittest.TestCase):
    def test_populated_tier_is_kept(self):
        self.assertEqual(
            logic.pick_available_tier("rare", {"common", "rare"}), "rare"
        )

    def test_empty_tier_downgrades_to_nearest_below(self):
        self.assertEqual(
            logic.pick_available_tier("epic", {"common", "uncommon"}), "uncommon"
        )

    def test_never_upgrades(self):
        self.assertIsNone(logic.pick_available_tier("common", {"epic", "legendary"}))


class StreakTests(unittest.TestCase):
    def test_consecutive_day_extends(self):
        self.assertEqual(
            logic.next_streak(3, date(2026, 7, 2), date(2026, 7, 3)), 4
        )

    def test_same_day_scan_does_not_extend(self):
        self.assertEqual(
            logic.next_streak(3, date(2026, 7, 3), date(2026, 7, 3)), 3
        )

    def test_gap_resets_to_one(self):
        self.assertEqual(
            logic.next_streak(9, date(2026, 7, 1), date(2026, 7, 3)), 1
        )

    def test_first_scan_ever_starts_at_one(self):
        self.assertEqual(logic.next_streak(0, None, date(2026, 7, 3)), 1)

    def test_effective_streak_dies_after_missed_day(self):
        self.assertEqual(
            logic.effective_streak(9, date(2026, 7, 1), date(2026, 7, 3)), 0
        )
        self.assertEqual(
            logic.effective_streak(9, date(2026, 7, 2), date(2026, 7, 3)), 9
        )


class AllowanceTests(unittest.TestCase):
    CFG = {"base": 3, "streak_min_days": 7, "streak_amount": 4}

    def test_base_allowance(self):
        self.assertEqual(
            logic.daily_allowance(2, date(2026, 7, 2), date(2026, 7, 3), self.CFG), 3
        )

    def test_streak_bonus_at_seven(self):
        self.assertEqual(
            logic.daily_allowance(7, date(2026, 7, 2), date(2026, 7, 3), self.CFG), 4
        )

    def test_dead_streak_gets_base_even_if_long(self):
        self.assertEqual(
            logic.daily_allowance(30, date(2026, 6, 25), date(2026, 7, 3), self.CFG), 3
        )


class _FakeConn:
    """Just enough of a psycopg connection for load_config."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kw):
        class _Cur:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _Cur(self._rows)


class RateConfigTests(unittest.TestCase):
    def test_db_rows_override_defaults(self):
        conn = _FakeConn([("tier_weights", {"common": 99, "legendary": 1})])
        cfg = load_config(conn)
        self.assertEqual(cfg["tier_weights"]["common"], 99)
        # Untouched keys still present via defaults:
        self.assertEqual(cfg["pity"]["epic"], 20)

    def test_missing_rows_fall_back_to_defaults(self):
        cfg = load_config(_FakeConn([]))
        self.assertEqual(cfg["tier_weights"]["legendary"], 1)
        self.assertEqual(cfg["daily_scans"]["base"], 3)


class DupeFluxTests(unittest.TestCase):
    def test_values_by_tier(self):
        cfg = {"common": 5, "uncommon": 15, "rare": 40, "epic": 100, "legendary": 250}
        self.assertEqual(logic.dupe_flux("legendary", cfg), 250)
        self.assertEqual(logic.dupe_flux("common", cfg), 5)


class IncomeTests(unittest.TestCase):
    CFG = {
        "daily_by_tier": {"common": 1, "uncommon": 2, "rare": 5, "epic": 12, "legendary": 30},
        "duplicate_bonus": 0.25,
        "duplicate_bonus_cap": 4,
        "income_bonus_per_level": 0.5,
    }

    def test_first_copy_gets_full_daily_income(self):
        self.assertEqual(logic.income_count_multiplier(1, self.CFG), 1.0)
        self.assertEqual(logic.daily_income_for_card("rare", 1, self.CFG), 5.0)

    def test_duplicate_income_has_bounded_bonus(self):
        self.assertEqual(logic.income_count_multiplier(2, self.CFG), 1.25)
        self.assertEqual(logic.income_count_multiplier(5, self.CFG), 2.0)
        self.assertEqual(logic.income_count_multiplier(99, self.CFG), 2.0)
        self.assertEqual(logic.daily_income_for_card("legendary", 5, self.CFG), 60.0)

    def test_level_increases_income(self):
        self.assertEqual(logic.income_level_multiplier(1, self.CFG), 1.0)
        self.assertEqual(logic.income_level_multiplier(3, self.CFG), 2.0)
        self.assertEqual(logic.daily_income_for_card("rare", 1, self.CFG, 3), 10.0)

    def test_upgrade_rules(self):
        cfg = {
            "max_level": 5,
            "cost_by_tier": {"common": 20, "legendary": 500},
        }
        self.assertEqual(logic.upgrade_cost("common", 1, cfg), 20)
        self.assertEqual(logic.upgrade_cost("legendary", 3, cfg), 1500)
        self.assertEqual(logic.can_upgrade_card(2, 1, cfg), (True, None))
        self.assertEqual(logic.can_upgrade_card(1, 1, cfg), (False, "needs_duplicate"))
        self.assertEqual(logic.can_upgrade_card(9, 5, cfg), (False, "max_level"))


if __name__ == "__main__":
    unittest.main()
