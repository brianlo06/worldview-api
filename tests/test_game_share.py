"""Game pull share cards: PNG renders from the share snapshot alone (the
source cluster may be pruned) and the landing page recruits into /game.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_game_share`
"""

from __future__ import annotations

import unittest

from worldview_api.share.card import render_card
from worldview_api.share.html import render_share_html
from worldview_api.share.store import Share


def _pull_share(**kw) -> Share:
    base = dict(
        id="pull123456", kind="pull", params={},
        title="New telescope images released",
        place="Chile", question=None, answer=None,
        fly_lat=-24.6, fly_lon=-70.4,
        stats={"tier": "legendary", "category": "ai",
               "pool_date": "2026-07-03", "art_seed": 12345},
    )
    base.update(kw)
    return Share(**base)


class PullCardTests(unittest.TestCase):
    def test_renders_png_from_snapshot_only(self):
        # No DB access happens in render_card — the Share dataclass IS the
        # snapshot, which is exactly the pruned-cluster survival property.
        data = render_card(_pull_share())
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 1000)

    def test_renders_all_tiers_and_missing_fields(self):
        for tier in ("common", "uncommon", "rare", "epic", "legendary"):
            data = render_card(_pull_share(stats={"tier": tier}, place=None))
            self.assertTrue(data.startswith(b"\x89PNG"), tier)


class PullHtmlTests(unittest.TestCase):
    def test_redirects_to_game(self):
        html = render_share_html(_pull_share())
        self.assertIn("/game", html)
        # og meta mentions the tier and the game
        self.assertIn("LEGENDARY", html)
        self.assertIn("jarvisworlds.com/game", html)

    def test_non_pull_shares_unaffected(self):
        share = _pull_share(kind="ask", question="what's happening?",
                            answer="Something.", stats={})
        html = render_share_html(share)
        self.assertNotIn("/game\"", html.split("refresh")[0])
        self.assertIn("WORLDVIEW", html)


if __name__ == "__main__":
    unittest.main()
