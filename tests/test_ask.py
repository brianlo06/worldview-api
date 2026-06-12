"""Tests for the 'ask the globe' pipeline (no DB / no live LLM required).

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_ask`

DB- and LLM-touching helpers are patched so these exercise normalization,
orchestration, the degraded path, cache-hit short-circuit, the budget, and
pre-baking in isolation.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from worldview_api.ask import answer as A
from worldview_api.ask import prebake
from worldview_api.ask.budget import budget
from worldview_api.config import settings


def _story(title="Quake hits port", summary="A magnitude 6 quake struck.", lat=35.0,
           lon=139.0, cc="JP", city="Tokyo", events=12, imp=0.8):
    return A.Story(id="11111111-1111-1111-1111-111111111111", title=title,
                   summary=summary, lat=lat, lon=lon, country_code=cc, city=city,
                   event_count=events, importance=imp)


class NormalizeTests(unittest.TestCase):
    def test_biggest_story_intent_collapses(self):
        for q in ["biggest story right now", "what's the top story?",
                  "What's happening right now"]:
            _, intent = A.normalize_question(q)
            self.assertEqual(intent, "biggest_story", q)

    def test_world_ok_intent(self):
        _, intent = A.normalize_question("is the world okay today?")
        self.assertEqual(intent, "world_ok")

    def test_near_me_buckets_coordinates(self):
        k1, i1 = A.normalize_question("what's happening near me", 35.04, 139.02)
        k2, _ = A.normalize_question("anything near me?", 35.01, 138.98)
        self.assertEqual(i1, "near_me")
        self.assertEqual(k1, k2)  # both bucket to the same rounded cell

    def test_generic_country_question(self):
        k, intent = A.normalize_question("what's happening in Ukraine?")
        self.assertEqual(intent, "country")
        self.assertTrue(k.endswith(":UP"))  # FIPS code for Ukraine

    def test_specific_question_stays_topical(self):
        # Mentions a country but is a specific question → semantic search, not
        # the generic country briefing.
        _, intent = A.normalize_question("did the ceasefire in israel hold")
        self.assertEqual(intent, "topical")

    def test_bare_city_view_is_near_me(self):
        _, intent = A.normalize_question("", 40.0, -74.0)
        self.assertEqual(intent, "near_me")


class DegradedAnswerTests(unittest.TestCase):
    def test_degraded_uses_top_summary(self):
        out = A._degraded_answer("q", [_story()])
        self.assertIn("Tokyo", out)
        self.assertIn("quake", out.lower())

    def test_degraded_empty_is_graceful(self):
        out = A._degraded_answer("q", [])
        self.assertTrue(out)
        self.assertNotIn("None", out)

    def test_degraded_maps_country_code_not_bare_iso(self):
        # Only a country_code (no city) → show the name, never the raw code.
        out = A._degraded_answer("q", [_story(city=None, cc="IL")])
        self.assertIn("Israel", out)
        self.assertNotIn("In IL,", out)

    def test_degraded_omits_unknown_code(self):
        out = A._degraded_answer("q", [_story(city=None, cc="ZZ")])
        self.assertFalse(out.startswith("In ZZ"))

    def test_degraded_pluralizes_developments(self):
        one = A._degraded_answer("q", [_story(), _story(title="b")])
        self.assertIn("1 related development is", one)
        many = A._degraded_answer("q", [_story(), _story(), _story()])
        self.assertIn("2 related developments are", many)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        budget._reset_for_tests()

    def test_cache_hit_short_circuits_retrieval_and_llm(self):
        cached = A.AnswerResult(answer="cached!", source="cache")
        with mock.patch.object(A, "cache_get", return_value=cached) as cg, \
             mock.patch.object(A, "retrieve_clusters") as rc, \
             mock.patch.object(A, "_synthesize") as synth:
            res = A.answer_question("anything topical")
        self.assertEqual(res.answer, "cached!")
        cg.assert_called_once()
        rc.assert_not_called()
        synth.assert_not_called()

    def test_degrades_when_llm_returns_none(self):
        with mock.patch.object(A, "cache_get", return_value=None), \
             mock.patch.object(A, "cache_put") as cp, \
             mock.patch.object(A, "retrieve_clusters", return_value=[_story()]), \
             mock.patch.object(A, "_synthesize", return_value=None):
            res = A.answer_question("some topical question")
        self.assertEqual(res.source, "degraded")
        self.assertEqual(res.fly_lat, 35.0)
        self.assertEqual(res.cluster_refs, ["11111111-1111-1111-1111-111111111111"])
        cp.assert_called_once()  # degraded answers are still cached

    def test_live_answer_when_llm_succeeds(self):
        with mock.patch.object(A, "cache_get", return_value=None), \
             mock.patch.object(A, "cache_put"), \
             mock.patch.object(A, "retrieve_clusters", return_value=[_story()]), \
             mock.patch.object(A, "_synthesize", return_value="A calm synthesized line."):
            res = A.answer_question("some topical question")
        self.assertEqual(res.source, "live")
        self.assertEqual(res.answer, "A calm synthesized line.")

    def test_no_match_returns_graceful_no_flyto(self):
        with mock.patch.object(A, "cache_get", return_value=None), \
             mock.patch.object(A, "cache_put"), \
             mock.patch.object(A, "retrieve_clusters", return_value=[]):
            res = A.answer_question("zxqwv nonsense")
        self.assertIsNone(res.fly_lat)
        self.assertEqual(res.stats.get("stories"), 0)
        self.assertTrue(res.answer)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        budget._reset_for_tests()
        self._cap = settings.ask_llm_daily_cap
        self._rpm = settings.ask_llm_max_rpm

    def tearDown(self):
        settings.ask_llm_daily_cap = self._cap
        settings.ask_llm_max_rpm = self._rpm
        budget._reset_for_tests()

    def test_cap_then_degrade(self):
        settings.ask_llm_daily_cap = 3
        settings.ask_llm_max_rpm = 60000  # ~1ms interval; sleep clears the pacer
        granted = 0
        for _ in range(6):
            if budget.try_acquire():
                granted += 1
            time.sleep(0.003)
        self.assertEqual(granted, 3)
        self.assertFalse(budget.try_acquire())

    def test_pace_blocks_rapid_calls(self):
        settings.ask_llm_daily_cap = 100
        settings.ask_llm_max_rpm = 60  # 1s interval
        self.assertTrue(budget.try_acquire())
        self.assertFalse(budget.try_acquire())  # immediate second call → degrade

    def test_synthesize_degrades_when_budget_spent(self):
        settings.ask_llm_daily_cap = 0  # nothing available
        out = A._synthesize("q", [_story()], use_budget=True)
        self.assertIsNone(out)


class PrebakeTests(unittest.TestCase):
    def test_prebake_writes_prebaked_source_without_interactive_budget(self):
        budget._reset_for_tests()
        writes: list[tuple] = []

        def _capture(key, question, result, source):
            writes.append((key, source, result.source))

        with mock.patch.object(prebake.A, "retrieve_top_clusters", return_value=[_story()]), \
             mock.patch.object(prebake.A, "retrieve_top_by_country", return_value=[_story()]), \
             mock.patch.object(prebake, "_top_countries", return_value=["US", "UA"]), \
             mock.patch.object(prebake.A, "_synthesize", return_value="baked line") as synth, \
             mock.patch.object(prebake.A, "cache_put", side_effect=_capture):
            out = prebake.prebake_once()

        self.assertEqual(out["baked"], 4)  # 2 global + 2 countries
        self.assertTrue(all(src == "prebaked" for _, src, _ in writes))
        # Pre-baking must NOT draw on the interactive budget.
        for call in synth.call_args_list:
            self.assertFalse(call.kwargs.get("use_budget", True))
        # Interactive budget untouched.
        self.assertEqual(budget.stats()["spent"], 0)


if __name__ == "__main__":
    unittest.main()
