"""Integration tests for POST /game/scan and GET /game/collection.

Uses the local dev DB (skips if unreachable). Pool rows are inserted directly
into game_card_pool — the scan path never touches clusters/events, which is
itself the snapshot property under test.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_game_scan`
"""

from __future__ import annotations

import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone


def _db_available() -> bool:
    try:
        from worldview_api.db import get_pool
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@unittest.skipUnless(_db_available(), "local dev DB not reachable")
class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from worldview_api.api import app
        cls.client = TestClient(app)

    def setUp(self):
        from worldview_api.db import get_pool
        from worldview_api.game import wallet as wallet_store
        self.pool = get_pool()
        self._orig_today_utc = wallet_store.today_utc
        self.today = date(2099, 1, 2)
        wallet_store.today_utc = lambda: self.today
        # Pool dated *yesterday* with no pool for today: scans must fall back
        # to the latest pool <= today (the 00:00–00:05 mint-gap behavior).
        self.pool_date = self.today - timedelta(days=1)
        cards = [
            ("common", "T1", "politics"), ("common", "T2", "economy"),
            ("common", "T3", "science"), ("uncommon", "T4", "sports"),
            ("rare", "T5", "politics"), ("legendary", "T6", "ai"),
        ]
        self.card_ids = []
        with self.pool.connection() as conn:
            for tier, country, category in cards:
                (cid,) = conn.execute(
                    "INSERT INTO game_card_pool (pool_date, source_cluster_id, "
                    " tier, headline, summary, lat, lon, country, category, "
                    " importance, art_seed) "
                    "VALUES (%s, %s, %s, %s, 's', 10, 20, %s, %s, 0.5, 42) "
                    "RETURNING id",
                    (self.pool_date, uuid.uuid4(), tier,
                     f"SCANTEST {tier} {country}", country, category),
                ).fetchone()
                self.card_ids.append(cid)
            conn.commit()

        r = self.client.post("/game/player", json={})
        assert r.status_code == 201, r.text
        self.player_id = r.json()["player_id"]
        self.headers = {"X-Player-Token": r.json()["token"]}

    def tearDown(self):
        from worldview_api.game import wallet as wallet_store
        wallet_store.today_utc = self._orig_today_utc
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM game_income_log WHERE player_id = %s", (self.player_id,))
            conn.execute("DELETE FROM game_pulls WHERE player_id = %s", (self.player_id,))
            conn.execute("DELETE FROM game_inventory WHERE player_id = %s", (self.player_id,))
            conn.execute("DELETE FROM game_badges WHERE player_id = %s", (self.player_id,))
            conn.execute("DELETE FROM game_wallet WHERE player_id = %s", (self.player_id,))
            conn.execute("DELETE FROM game_players WHERE id = %s", (self.player_id,))
            conn.execute("DELETE FROM game_card_pool WHERE headline LIKE 'SCANTEST%%'")
            conn.commit()

    def _set_scans(self, n: int):
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE game_wallet SET scans_left = %s, scans_granted_day = %s "
                "WHERE player_id = %s",
                (n, self.today, self.player_id),
            )
            conn.commit()

    def _set_wallet_flux(self, n: int):
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE game_wallet SET flux = %s WHERE player_id = %s",
                (n, self.player_id),
            )
            conn.commit()

    def test_scan_uses_latest_pool_before_todays_mint(self):
        r = self.client.post("/game/scan", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["card"]["pool_date"], str(self.pool_date))
        self.assertTrue(body["card"]["headline"].startswith("SCANTEST"))
        self.assertEqual(body["wallet"]["scans_left"], 2)
        self.assertEqual(body["streak_days"], 1)
        # pull is durably logged
        with self.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM game_pulls WHERE player_id = %s",
                (self.player_id,),
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_double_submit_with_one_scan_spends_once(self):
        self._set_scans(1)
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(self.client.post, "/game/scan", headers=self.headers)
                    for _ in range(2)]
            codes = sorted(f.result().status_code for f in futs)
        self.assertEqual(codes, [200, 429])
        with self.pool.connection() as conn:
            pulls = conn.execute(
                "SELECT count(*) FROM game_pulls WHERE player_id = %s",
                (self.player_id,),
            ).fetchone()[0]
        self.assertEqual(pulls, 1)

    def test_dupes_credit_flux_and_badges_award_once(self):
        self._set_scans(40)
        dupe_seen, flux_ok = False, True
        badge_awards: list[str] = []
        for _ in range(40):
            r = self.client.post("/game/scan", headers=self.headers)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            badge_awards.extend(body["new_badges"])
            if body["is_dupe"]:
                dupe_seen = True
                if body["flux_credit"] <= 0:
                    flux_ok = False
        # 40 pulls from a 6-card pool: dupes are certain.
        self.assertTrue(dupe_seen)
        self.assertTrue(flux_ok)
        # No badge key reported as new more than once across the session.
        self.assertEqual(len(badge_awards), len(set(badge_awards)))
        # 429 with reset time once empty
        r = self.client.post("/game/scan", headers=self.headers)
        self.assertEqual(r.status_code, 429)
        self.assertIn("reset_at", r.json()["detail"])

    def test_collection_reflects_pulls(self):
        self._set_scans(10)
        for _ in range(10):
            self.client.post("/game/scan", headers=self.headers)
        r = self.client.get("/game/collection", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["summary"]["total_pulls"], 10)
        self.assertGreaterEqual(len(body["cards"]), 1)
        counts = sum(c["count"] for c in body["cards"])
        self.assertEqual(counts, 10)
        for c in body["cards"]:
            self.assertTrue(c["headline"].startswith("SCANTEST"))
            self.assertIsNotNone(c["art_seed"])

    def test_passive_income_claim_credits_flux(self):
        earned_at = datetime.now(timezone.utc) - timedelta(days=2)
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO game_inventory (player_id, card_id, tier, count, first_at) "
                "VALUES (%s, %s, 'common', 1, %s)",
                (self.player_id, self.card_ids[0], earned_at),
            )
            conn.execute(
                "UPDATE game_wallet SET income_claimed_at = %s WHERE player_id = %s",
                (earned_at, self.player_id),
            )
            conn.commit()

        r = self.client.post("/game/income/claim", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["claimed"], 1)
        self.assertEqual(body["wallet"]["flux"], body["claimed"])

        r = self.client.get("/game/collection", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("income_ready", r.json()["summary"])
        self.assertEqual(r.json()["cards"][0]["income_per_day"], 1)
        self.assertEqual(r.json()["cards"][0]["level"], 1)

    def test_flux_can_buy_bonus_scan_when_free_scans_empty(self):
        self._set_scans(0)
        self._set_wallet_flux(60)
        r = self.client.post(
            "/game/scan",
            headers=self.headers,
            json={"payment": "flux"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["flux_spent"], 60)
        self.assertEqual(body["wallet"]["scans_left"], 0)
        self.assertEqual(body["wallet"]["flux"], 0)

    def test_duplicate_and_flux_upgrade_card_income(self):
        earned_at = datetime.now(timezone.utc) - timedelta(days=2)
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO game_inventory (player_id, card_id, tier, count, level, first_at) "
                "VALUES (%s, %s, 'common', 2, 1, %s)",
                (self.player_id, self.card_ids[0], earned_at),
            )
            conn.execute(
                "UPDATE game_wallet SET flux = 20 WHERE player_id = %s",
                (self.player_id,),
            )
            conn.commit()

        r = self.client.post(
            f"/game/cards/{self.card_ids[0]}/upgrade",
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["level"], 2)
        self.assertEqual(body["flux_spent"], 20)
        self.assertEqual(body["wallet"]["flux"], 0)
        self.assertEqual(body["income_per_day"], 1.88)

        r = self.client.get("/game/collection", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        card = r.json()["cards"][0]
        self.assertEqual(card["level"], 2)
        self.assertFalse(card["can_upgrade"])
        self.assertIsNone(card["upgrade_cost"])


if __name__ == "__main__":
    unittest.main()
