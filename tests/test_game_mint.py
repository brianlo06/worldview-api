"""Tests for the daily card-pool mint.

Pure tiering/filter tests need no DB. The integration tests (idempotency,
sparse fallback, grim exclusion end-to-end) use the local dev database with a
sandbox pool_date and clean up after themselves; they skip if no DB is
reachable.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_game_mint`
"""

from __future__ import annotations

import unittest
import uuid
from datetime import date

from worldview_api.game.mint import (
    art_seed_for,
    assign_tiers,
    dedupe_candidates,
    is_grim,
    rare_countries,
)

MINT_CFG = {"importance_floor": 0.45, "pool_cap": 120, "sparse_min": 40, "legendary_max": 3}


def _cards(n: int, **overrides) -> list[dict]:
    cards = []
    for i in range(n):
        c = {
            "source_cluster_id": str(uuid.uuid4()),
            "headline": f"Story {i}",
            "summary": "s",
            "lat": 0.0,
            "lon": 0.0,
            "country": f"C{i % 10}",
            "category": ["politics", "economy", "science", "sports"][i % 4],
            "importance": 1.0 - i / n,
            "geo_precision": "city",
        }
        c.update(overrides)
        cards.append(c)
    return cards


class GrimFilterTests(unittest.TestCase):
    TERMS = ["killed", "death toll", "massacre"]

    def test_matches_case_insensitively(self):
        self.assertTrue(is_grim("Dozens KILLED in flood", self.TERMS))
        self.assertTrue(is_grim("Death Toll rises", self.TERMS))

    def test_clean_headline_passes(self):
        self.assertFalse(is_grim("Rocket launch succeeds", self.TERMS))

    def test_none_passes(self):
        self.assertFalse(is_grim(None, self.TERMS))


class ArtSeedTests(unittest.TestCase):
    def test_deterministic_and_positive(self):
        cid = str(uuid.uuid4())
        self.assertEqual(art_seed_for(cid), art_seed_for(cid))
        self.assertGreaterEqual(art_seed_for(cid), 0)
        self.assertLess(art_seed_for(cid), 2**63)

    def test_different_ids_differ(self):
        self.assertNotEqual(art_seed_for("a"), art_seed_for("b"))


class RareCountryTests(unittest.TestCase):
    def test_bottom_quartile_is_rare(self):
        freq = {f"C{i}": (i + 1) * 10 for i in range(16)}
        rare = rare_countries(freq)
        self.assertIn("C0", rare)
        self.assertNotIn("C15", rare)

    def test_no_history_means_no_rarity(self):
        self.assertEqual(rare_countries({"US": 5}), set())


class DedupeTests(unittest.TestCase):
    def test_keeps_best_per_country_category(self):
        a = _cards(1, country="US", category="politics", importance=0.9)[0]
        b = _cards(1, country="US", category="politics", importance=0.5)[0]
        c = _cards(1, country="FR", category="politics", importance=0.4)[0]
        out = dedupe_candidates([b, a, c], cap=10)
        self.assertIn(a, out)
        self.assertNotIn(b, out)
        self.assertIn(c, out)

    def test_cap_applies(self):
        out = dedupe_candidates(_cards(50), cap=10)
        self.assertEqual(len(out), 10)


class TierAssignTests(unittest.TestCase):
    def test_every_card_gets_a_valid_tier(self):
        cards = _cards(100)
        assign_tiers(cards, {}, MINT_CFG)
        from worldview_api.game.logic import TIER_ORDER
        self.assertTrue(all(c["tier"] in TIER_ORDER for c in cards))

    def test_commons_dominate(self):
        cards = _cards(100)
        assign_tiers(cards, {}, MINT_CFG)
        commons = sum(1 for c in cards if c["tier"] == "common")
        self.assertGreater(commons, 40)

    def test_legendary_count_bounded(self):
        for n in (10, 50, 120):
            cards = _cards(n)
            assign_tiers(cards, {}, MINT_CFG)
            legs = sum(1 for c in cards if c["tier"] == "legendary")
            self.assertGreaterEqual(legs, 1, f"n={n}")
            self.assertLessEqual(legs, MINT_CFG["legendary_max"], f"n={n}")

    def test_top_importance_rare_country_reaches_epic_or_legendary(self):
        freq = {f"C{i}": (i + 1) * 10 for i in range(16)}
        cards = _cards(100)
        cards[0]["country"] = "C0"  # rare band; card 0 has top importance
        assign_tiers(cards, freq, MINT_CFG)
        self.assertIn(cards[0]["tier"], ("epic", "legendary"))

    def test_scarce_category_bumps_one_band(self):
        # 99 politics cards + 1 science card mid-pool: science is scarce.
        cards = _cards(100, category="politics")
        cards[50]["category"] = "science"
        plain = [dict(c) for c in cards]
        assign_tiers(plain, {}, MINT_CFG)
        assign_tiers(cards, {}, MINT_CFG)
        from worldview_api.game.logic import TIER_ORDER
        self.assertGreaterEqual(
            TIER_ORDER.index(cards[50]["tier"]),
            TIER_ORDER.index(plain[50]["tier"]),
        )


def _db_available() -> bool:
    try:
        from worldview_api.db import get_pool
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@unittest.skipUnless(_db_available(), "local dev DB not reachable")
class MintIntegrationTests(unittest.TestCase):
    """End-to-end mint against synthetic clusters in the dev DB."""

    POOL_DATE = date(2000, 1, 1)  # sandbox date, never collides with real pools
    MARKER = "GAMETEST::"

    def setUp(self):
        from worldview_api.db import get_pool
        self.pool = get_pool()
        zero_vec = "[" + ",".join(["0"] * 384) + "]"
        self.cluster_ids = []
        headlines = [
            (self.MARKER + "Rocket launch succeeds", 0.9),
            (self.MARKER + "Trade summit concludes", 0.8),
            (self.MARKER + "Dozens killed in flood", 0.85),  # grim → excluded
            (self.MARKER + "New telescope images released", 0.7),
            (self.MARKER + "Rail line opens", 0.6),
        ]
        with self.pool.connection() as conn:
            for i, (title, imp) in enumerate(headlines):
                (eid,) = conn.execute(
                    "INSERT INTO events (title, url_hash, source, occurred_at, "
                    " location, country_code, geo_precision, summary) "
                    "VALUES (%s, %s, 'gametest', NOW(), "
                    " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, 'city', 's') "
                    "RETURNING id",
                    (title, f"gametest-{uuid.uuid4()}", 10.0 + i, 50.0, f"T{i}"),
                ).fetchone()
                (cid,) = conn.execute(
                    "INSERT INTO clusters (title, summary, first_seen, last_seen, "
                    " event_count, centroid_embedding, primary_country, "
                    " primary_category, importance_score, representative_event_id) "
                    "VALUES (%s, 's', NOW(), NOW(), 1, %s, %s, 'science', %s, %s) "
                    "RETURNING id",
                    (title, zero_vec, f"T{i}", imp, eid),
                ).fetchone()
                self.cluster_ids.append(cid)
            conn.commit()

    def tearDown(self):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM game_card_pool WHERE pool_date = %s", (self.POOL_DATE,))
            conn.execute("DELETE FROM clusters WHERE title LIKE %s", (self.MARKER + "%",))
            conn.execute("DELETE FROM events WHERE title LIKE %s", (self.MARKER + "%",))
            conn.execute("DELETE FROM game_country_freq WHERE country LIKE 'T%%'")
            conn.commit()

    def test_mint_excludes_grim_and_is_idempotent_with_fallback(self):
        from worldview_api.game.mint import mint_pool

        stats = mint_pool(self.POOL_DATE)
        # 5 synthetic candidates < sparse_min → 72h fallback window
        self.assertEqual(stats["window_h"], 72)
        self.assertGreaterEqual(stats["excluded_grim"], 1)
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT headline, tier FROM game_card_pool WHERE pool_date = %s",
                (self.POOL_DATE,),
            ).fetchall()
        headlines = [r[0] for r in rows]
        self.assertTrue(any("Rocket launch" in h for h in headlines))
        self.assertFalse(any("killed" in h.lower() for h in headlines))
        # snapshot completeness: every row renderable without clusters
        with self.pool.connection() as conn:
            incomplete = conn.execute(
                "SELECT count(*) FROM game_card_pool WHERE pool_date = %s "
                "AND (headline IS NULL OR lat IS NULL OR lon IS NULL OR tier IS NULL)",
                (self.POOL_DATE,),
            ).fetchone()[0]
        self.assertEqual(incomplete, 0)

        # idempotency: second run inserts nothing new
        stats2 = mint_pool(self.POOL_DATE)
        self.assertEqual(stats2["inserted"], 0)


if __name__ == "__main__":
    unittest.main()
