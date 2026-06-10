"""Tests for the top-stories briefing narration (no DB / no live LLM required).

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_briefing`

Exercises the speech cleaner, the no-LLM fallback, empty-selection handling, the
isolated budget, and cluster_id reconciliation in isolation.
"""

from __future__ import annotations

import unittest
from unittest import mock

import httpx
import openai

from worldview_api.briefing import narrate as N
from worldview_api.briefing.budget import budget
from worldview_api.config import settings


def _rate_limit_error(msg="Please retry in 2.2s"):
    req = httpx.Request("POST", "http://x")
    return openai.RateLimitError(msg, response=httpx.Response(429, request=req), body=None)


_OK_JSON = (
    '{"intro":"Top stories.","stories":[{"cluster_id":"t1",'
    '"narration":"A severe storm is moving through Omaha tonight."}],"outro":"Done."}'
)


class _FakeClient:
    """Stand-in OpenAI client whose create() 429s `fail_times` then succeeds."""

    def __init__(self, fail_times):
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls += 1
                if outer.calls <= fail_times:
                    raise _rate_limit_error()
                msg = type("M", (), {"content": _OK_JSON})()
                choice = type("C", (), {"message": msg})()
                return type("R", (), {"choices": [choice]})()

        self.calls = 0
        self.chat = type("Chat", (), {"completions": _Completions()})()


# A condensed version of the NWS severe-thunderstorm text from the briefing card.
_NWS = (
    "SVRTOP The National Weather Service in Topeka has issued a * Severe "
    "Thunderstorm Warning for... Southeastern Anderson County in east central "
    "Kansas... * Until 115 AM CDT. * At 1236 AM CDT, a severe thunderstorm was "
    "located over Colony, moving east at 25 mph."
)


def _story(cid="c1", title="Severe Thunderstorm Warning issued by NWS Topeka",
           summary=_NWS, city="Topeka", cc="US") -> N.BriefingInput:
    return {"cluster_id": cid, "title": title, "summary": summary,
            "city": city, "country_code": cc}


class CleanForSpeechTests(unittest.TestCase):
    def test_strips_codes_markup_and_ellipses(self):
        out = N.clean_for_speech(_NWS)
        self.assertNotIn("SVRTOP", out)
        self.assertNotIn("*", out)
        self.assertNotIn("...", out)
        self.assertNotIn("…", out)
        self.assertTrue(out)  # still produced something speakable

    def test_empty_input(self):
        self.assertEqual(N.clean_for_speech(None), "")
        self.assertEqual(N.clean_for_speech(""), "")

    def test_truncates_long_text(self):
        long = "word " * 200
        out = N.clean_for_speech(long, max_chars=50)
        self.assertLessEqual(len(out), 51)  # +1 for the ellipsis


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self._key = settings.llm_api_key
        settings.llm_api_key = None  # force the no-LLM path
        budget._reset_for_tests()

    def tearDown(self):
        settings.llm_api_key = self._key

    def test_empty_selection_skips_llm(self):
        script, source = N.generate_briefing([])
        self.assertEqual(source, "fallback")
        self.assertEqual(script["stories"], [])

    def test_fallback_when_llm_disabled(self):
        script, source = N.generate_briefing([_story()])
        self.assertEqual(source, "fallback")
        self.assertEqual(len(script["stories"]), 1)
        narration = script["stories"][0]["narration"]
        self.assertTrue(narration)
        self.assertNotIn("SVRTOP", narration)
        self.assertNotIn("*", narration)
        self.assertIn("Topeka", narration)  # location lead
        self.assertTrue(script["intro"])
        self.assertTrue(script["outro"])

    def test_budget_exhaustion_degrades(self):
        settings.llm_api_key = "sk-fake"  # would otherwise attempt the LLM
        cap = settings.briefing_llm_daily_cap
        settings.briefing_llm_daily_cap = 0  # nothing available → degrade
        try:
            _, source = N.generate_briefing([_story()])
            self.assertEqual(source, "fallback")
        finally:
            settings.briefing_llm_daily_cap = cap


class ReconcileTests(unittest.TestCase):
    def test_missing_id_filled_from_fallback_in_order(self):
        stories = [_story("a", title="Quake hits port", summary="A quake struck."),
                   _story("b", title="Floods in valley", summary="Rivers crested.")]
        # Model returned only one of the two ids, plus an invented one.
        parsed = N._BriefingScript(
            intro="Here are the top stories.",
            stories=[
                N._NarrationStory(cluster_id="b", narration="Floodwaters rose overnight."),
                N._NarrationStory(cluster_id="zzz", narration="Invented story."),
            ],
            outro="That's your briefing.",
        )
        out = N._reconcile(parsed, stories)
        ids = [s["cluster_id"] for s in out["stories"]]
        self.assertEqual(ids, ["a", "b"])  # requested order preserved, no invented id
        # 'a' was missing → filled from fallback (non-empty, cleaned)
        self.assertTrue(out["stories"][0]["narration"])
        # 'b' kept the model's narration
        self.assertEqual(out["stories"][1]["narration"], "Floodwaters rose overnight.")

    def test_blank_narration_filled(self):
        stories = [_story("a", title="Quake hits port", summary="A quake struck.")]
        parsed = N._BriefingScript(
            intro="", stories=[N._NarrationStory(cluster_id="a", narration="   ")],
            outro="",
        )
        out = N._reconcile(parsed, stories)
        self.assertTrue(out["stories"][0]["narration"].strip())
        self.assertTrue(out["intro"])  # blank intro → default
        self.assertTrue(out["outro"])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self._key = settings.llm_api_key
        settings.llm_api_key = "sk-fake"
        budget._reset_for_tests()

    def tearDown(self):
        settings.llm_api_key = self._key

    def test_retry_after_parses_body_hint(self):
        secs = N._retry_after_seconds(_rate_limit_error("Quota exceeded. Please retry in 2.231s."))
        self.assertAlmostEqual(secs, 2.231, places=3)

    def test_one_429_then_success(self):
        with mock.patch.object(N, "_get_client", return_value=_FakeClient(fail_times=1)), \
             mock.patch.object(N.time, "sleep", return_value=None):
            script, source = N.generate_briefing([_story(cid="t1")])
        self.assertEqual(source, "llm")
        self.assertIn("Omaha", script["stories"][0]["narration"])

    def test_persistent_429_degrades_to_fallback(self):
        with mock.patch.object(N, "_get_client", return_value=_FakeClient(fail_times=99)), \
             mock.patch.object(N.time, "sleep", return_value=None):
            script, source = N.generate_briefing([_story()])
        self.assertEqual(source, "fallback")
        self.assertTrue(script["stories"][0]["narration"])  # still playable


class ParseTests(unittest.TestCase):
    def test_parses_fenced_json(self):
        raw = '```json\n{"intro":"Hi","stories":[{"cluster_id":"a","narration":"x"}],"outro":"Bye"}\n```'
        parsed = N._parse_script(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intro, "Hi")
        self.assertEqual(parsed.stories[0].cluster_id, "a")

    def test_returns_none_on_garbage(self):
        self.assertIsNone(N._parse_script("not json at all"))


if __name__ == "__main__":
    unittest.main()


class DiversifyTests(unittest.TestCase):
    """Per-category cap on the briefing's story selection (routers.briefing)."""

    def test_caps_dominant_category_and_keeps_order(self):
        from worldview_api.routers.briefing import _diversify

        rows = [
            {"category": "weather", "id": 1},
            {"category": "weather", "id": 2},
            {"category": "weather", "id": 3},
            {"category": "weather", "id": 4},
            {"category": "conflict", "id": 5},
            {"category": "politics", "id": 6},
        ]
        picked = _diversify(rows, 5)
        cats = [r["category"] for r in picked]
        # 2 weather (cap), then conflict + politics, then 1 weather backfill
        self.assertEqual(cats[:4], ["weather", "weather", "conflict", "politics"])
        self.assertEqual(len(picked), 5)
        self.assertEqual(cats.count("weather"), 3)

    def test_single_category_still_fills_the_briefing(self):
        from worldview_api.routers.briefing import _diversify

        rows = [{"category": "weather", "id": i} for i in range(8)]
        picked = _diversify(rows, 5)
        self.assertEqual(len(picked), 5)

    def test_none_category_grouped_as_uncategorized(self):
        from worldview_api.routers.briefing import _diversify

        rows = [{"category": None, "id": i} for i in range(4)]
        picked = _diversify(rows, 3)
        self.assertEqual(len(picked), 3)


class LocationLabelTests(unittest.TestCase):
    def test_code_like_city_falls_back_to_country(self):
        story = {"cluster_id": "x", "title": "t", "summary": None,
                 "city": "SF", "country_code": "ZA"}
        self.assertEqual(N._location_label(story), "South Africa")

    def test_real_city_used(self):
        story = {"cluster_id": "x", "title": "t", "summary": None,
                 "city": "Johannesburg", "country_code": "ZA"}
        self.assertEqual(N._location_label(story), "Johannesburg")
