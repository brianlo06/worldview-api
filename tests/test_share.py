"""Tests for the share service (no DB required for card/html/sanitize).

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_share`
"""

from __future__ import annotations

import unittest

from worldview_api.share import sanitize_text
from worldview_api.share.card import render_card
from worldview_api.share.html import render_share_html
from worldview_api.share.store import Share


def _share(**kw) -> Share:
    base = dict(
        id="abc123XYz0", kind="ask", params={"ask": "abc123XYz0"},
        title="Quake hits Tokyo port", place="Tokyo",
        question="what's happening in Japan?",
        answer="A magnitude 6 quake struck a port near Tokyo; no tsunami warning issued.",
        fly_lat=35.0, fly_lon=139.0, stats={"event_count": 12, "sources": 4},
    )
    base.update(kw)
    return Share(**base)


class SanitizeTests(unittest.TestCase):
    def test_strips_control_and_angle_chars(self):
        out = sanitize_text("hello\x00 <script>world</script>\nthere")
        self.assertNotIn("<", out)
        self.assertNotIn("\x00", out)
        self.assertIn("hello", out)

    def test_length_caps_with_ellipsis(self):
        out = sanitize_text("x" * 500, max_len=50)
        self.assertEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))

    def test_none_is_empty(self):
        self.assertEqual(sanitize_text(None), "")


class CardTests(unittest.TestCase):
    def test_renders_png_bytes(self):
        data = render_card(_share())
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 1000)

    def test_renders_with_missing_fields(self):
        # A bare 'view' share with no answer/place/coords must still render.
        data = render_card(_share(
            question=None, place=None, answer=None, fly_lat=None, fly_lon=None,
            title=None, stats={},
        ))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))


class HtmlTests(unittest.TestCase):
    def test_meta_and_card_present(self):
        html_doc = render_share_html(_share())
        self.assertIn('property="og:image"', html_doc)
        self.assertIn("/s/abc123XYz0/card.png", html_doc)
        self.assertIn('name="twitter:card" content="summary_large_image"', html_doc)
        # The answer flows into og:description.
        self.assertIn("magnitude 6 quake", html_doc)

    def test_human_redirect_into_spa_deeplink(self):
        html_doc = render_share_html(_share())
        self.assertIn("http-equiv=\"refresh\"", html_doc)
        self.assertIn("location.replace(", html_doc)
        self.assertIn("ask=abc123XYz0", html_doc)  # deep-link param preserved

    def test_escapes_quotes_in_question(self):
        html_doc = render_share_html(_share(question='war "escalates" now'))
        self.assertNotIn('content=""', html_doc)
        self.assertIn("&quot;", html_doc)


if __name__ == "__main__":
    unittest.main()
