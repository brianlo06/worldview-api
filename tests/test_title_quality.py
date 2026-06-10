"""Tests for URL-derived title placeholders rejecting alpha-free junk.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_title_quality`
"""

from __future__ import annotations

import unittest

from worldview_api.ingest.gdelt import _has_alpha, humanize_url


class HasAlphaTests(unittest.TestCase):
    def test_numbers_and_punctuation_have_no_alpha(self):
        for s in ["3952366", "1236931217", "  ", "-_-", "123-456", "%20%20"]:
            with self.subTest(s=s):
                self.assertFalse(_has_alpha(s))

    def test_latin_text_has_alpha(self):
        self.assertTrue(_has_alpha("Trump Xi Meeting"))
        self.assertTrue(_has_alpha("a1"))

    def test_non_latin_text_has_alpha(self):
        # Unicode-aware: Turkish, Arabic, Chinese headlines are real titles.
        self.assertTrue(_has_alpha("Cumhurbaşkanı açıklama yaptı"))  # Turkish
        self.assertTrue(_has_alpha("الرئيس يعلن"))                    # Arabic
        self.assertTrue(_has_alpha("总统发表讲话"))                    # Chinese


class HumanizeUrlTests(unittest.TestCase):
    def test_numeric_slug_falls_back_to_outlet_not_number(self):
        title, outlet = humanize_url("https://www.aa.com.tr/en/world/abc/3952366")
        self.assertEqual(outlet, "aa.com.tr")
        self.assertNotEqual(title, "3952366")
        self.assertNotIn("3952366", title)
        self.assertTrue(_has_alpha(title))  # the fallback itself is a real string

    def test_numeric_slug_with_extension(self):
        title, _ = humanize_url("https://motorsport.com/news/10825616.html")
        self.assertNotIn("10825616", title)
        self.assertTrue(_has_alpha(title))

    def test_word_slug_is_humanized(self):
        title, outlet = humanize_url("https://example.com/world/trump-xi-meeting")
        self.assertEqual(title, "Trump Xi Meeting")
        self.assertEqual(outlet, "example.com")

    def test_empty_path_falls_back_to_outlet(self):
        title, outlet = humanize_url("https://example.com/")
        self.assertEqual(outlet, "example.com")
        self.assertTrue(_has_alpha(title))


if __name__ == "__main__":
    unittest.main()


class JunkTitleTests(unittest.TestCase):
    """Section-page titles ("World") and brand titles are junk, headlines aren't."""

    def test_generic_section_titles_are_junk(self):
        from worldview_api.ingest.gdelt_gkg import _is_junk_title

        for t in ("World", "world", "Top Stories", "Breaking News", "OPINION", "Middle East"):
            self.assertTrue(_is_junk_title(t, "arabnews.com"), t)

    def test_real_headlines_are_not_junk(self):
        from worldview_api.ingest.gdelt_gkg import _is_junk_title

        for t in (
            "World leaders meet in Geneva over ceasefire proposal",
            "US and Iran launch airstrikes after escalation",
            "World Cup draw announced",
        ):
            self.assertFalse(_is_junk_title(t, "arabnews.com"), t)

    def test_brand_only_title_still_caught(self):
        from worldview_api.ingest.gdelt_gkg import _is_junk_title

        self.assertTrue(_is_junk_title("Deadline", "deadline.com"))


class CleanLocShortTests(unittest.TestCase):
    def test_two_letter_codes_dropped(self):
        from worldview_api.ingest.gdelt_gkg import _clean_loc_short

        self.assertIsNone(_clean_loc_short("SF", "state"))
        self.assertEqual(_clean_loc_short("Johannesburg, Gauteng, South Africa", "city"), "Johannesburg")
