"""Briefing hologram generation (no disk-shared state / no live API)."""

from __future__ import annotations

import base64
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from worldview_api.briefing import holo as H
from worldview_api.config import settings


def _gemini_response(png: bytes) -> mock.Mock:
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "rendered"},
                        {"inlineData": {"mimeType": "image/png",
                                        "data": base64.b64encode(png).decode()}},
                    ]
                }
            }
        ]
    }
    return resp


STORY = {
    "cluster_id": "11111111-1111-1111-1111-111111111111",
    "title": "Severe flooding displaces thousands in coastal city",
    "summary": "Rising waters forced evacuations overnight... officials say...",
    "category": "disaster",
}


class PromptTests(unittest.TestCase):
    def test_prompt_carries_story_and_style(self):
        p = H.build_prompt(STORY["title"], STORY["summary"], STORY["category"])
        self.assertIn("Severe flooding displaces thousands", p)
        self.assertIn("hologram", p)
        self.assertIn("disaster", p)
        self.assertIn("No text", p)
        # Scene leads the prompt — style text first makes Flux draw a
        # literal "display" instead of the event.
        self.assertTrue(p.startswith("(disaster news) Severe flooding"))

    def test_prompt_prefers_llm_scene(self):
        p = H.build_prompt(
            STORY["title"], STORY["summary"], STORY["category"],
            scene="Floodwater surging through a city street at night.",
        )
        self.assertTrue(p.startswith("Floodwater surging"))
        self.assertNotIn("Severe flooding displaces", p)
        self.assertIn("hologram", p)

    def test_prompt_cleans_separators_and_dedupes_summary(self):
        p = H.build_prompt("Quake hits region", "Quake hits region", None)
        # Identical summary isn't repeated.
        self.assertEqual(p.count("Quake hits region"), 1)

    def test_prompt_survives_missing_fields(self):
        p = H.build_prompt(None, None, None)
        self.assertIn("hologram", p)


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir_patch = mock.patch.object(settings, "holo_dir", self._tmp.name)
        self._dir_patch.start()
        H.budget._reset_for_tests()

    def tearDown(self):
        self._dir_patch.stop()
        self._tmp.cleanup()

    def test_gemini_renders_and_writes_png(self):
        with mock.patch.object(settings, "holo_provider", "gemini"), \
             mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(H.httpx, "post",
                               return_value=_gemini_response(b"PNGDATA")):
            ok = H._generate_one(STORY)
        self.assertTrue(ok)
        path = H.hologram_path(STORY["cluster_id"])
        self.assertEqual(path.read_bytes(), b"PNGDATA")

    def test_pollinations_renders_and_writes_image(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.headers = {"content-type": "image/jpeg"}
        resp.content = b"JPEGDATA"
        with mock.patch.object(settings, "holo_provider", "pollinations"), \
             mock.patch.object(H.httpx, "get", return_value=resp) as get:
            ok = H._generate_one(STORY)
        self.assertTrue(ok)
        path = H.hologram_path(STORY["cluster_id"])
        self.assertEqual(path.read_bytes(), b"JPEGDATA")
        # Prompt rides in the URL path; seed is stable per cluster.
        url = get.call_args[0][0]
        self.assertIn("/image/", url)
        self.assertIn("hologram", urllib.parse.unquote(url))
        seed1 = get.call_args[1]["params"]["seed"]
        path.unlink()
        H.budget._reset_for_tests()  # clear the RPM pace for the retry
        with mock.patch.object(settings, "holo_provider", "pollinations"), \
             mock.patch.object(H.httpx, "get", return_value=resp) as get2:
            H._generate_one(STORY)
        self.assertEqual(seed1, get2.call_args[1]["params"]["seed"])

    def test_pollinations_non_image_response_no_file(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.headers = {"content-type": "text/html"}
        resp.content = b"<html>error</html>"
        with mock.patch.object(settings, "holo_provider", "pollinations"), \
             mock.patch.object(H.httpx, "get", return_value=resp):
            ok = H._generate_one(STORY)
        self.assertFalse(ok)
        self.assertFalse(H.hologram_path(STORY["cluster_id"]).exists())

    def test_pollinations_needs_no_api_key(self):
        with mock.patch.object(settings, "holo_provider", "pollinations"), \
             mock.patch.object(settings, "llm_api_key", None), \
             mock.patch.object(settings, "holo_api_key", ""), \
             mock.patch.object(H.threading, "Thread") as thread:
            H.schedule_generation([STORY])
        thread.assert_called_once()

    def test_existing_file_skips_api(self):
        path = H.hologram_path(STORY["cluster_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"OLD")
        with mock.patch.object(H.httpx, "post") as post:
            ok = H._generate_one(STORY)
        self.assertTrue(ok)
        post.assert_not_called()
        self.assertEqual(path.read_bytes(), b"OLD")

    def test_budget_gate_blocks_render(self):
        with mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(H.budget, "try_acquire", return_value=False), \
             mock.patch.object(H.httpx, "post") as post, \
             mock.patch.object(H.httpx, "get") as get:
            ok = H._generate_one(STORY)
        self.assertFalse(ok)
        post.assert_not_called()
        get.assert_not_called()

    def test_api_error_swallowed_no_file(self):
        with mock.patch.object(settings, "holo_provider", "gemini"), \
             mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(H.httpx, "post", side_effect=RuntimeError("boom")):
            ok = H._generate_one(STORY)
        self.assertFalse(ok)
        self.assertFalse(H.hologram_path(STORY["cluster_id"]).exists())

    def test_refusal_no_image_part(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "cannot render"}]}}]
        }
        with mock.patch.object(settings, "holo_provider", "gemini"), \
             mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(H.httpx, "post", return_value=resp):
            ok = H._generate_one(STORY)
        self.assertFalse(ok)
        self.assertFalse(H.hologram_path(STORY["cluster_id"]).exists())

    def test_schedule_noops_without_key_for_gemini_or_when_disabled(self):
        with mock.patch.object(settings, "holo_provider", "gemini"), \
             mock.patch.object(settings, "llm_api_key", None), \
             mock.patch.object(settings, "holo_api_key", ""), \
             mock.patch.object(H.threading, "Thread") as thread:
            H.schedule_generation([STORY])
        thread.assert_not_called()
        with mock.patch.object(settings, "holo_enabled", False), \
             mock.patch.object(H.threading, "Thread") as thread:
            H.schedule_generation([STORY])
        thread.assert_not_called()

    def test_schedule_skips_existing_and_inflight(self):
        path = H.hologram_path(STORY["cluster_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"DONE")
        with mock.patch.object(settings, "llm_api_key", "k"), \
             mock.patch.object(H.threading, "Thread") as thread:
            H.schedule_generation([STORY])
        thread.assert_not_called()

    def test_prune_removes_only_old_files(self):
        root = Path(self._tmp.name)
        old = root / "old.png"
        new = root / "new.png"
        old.write_bytes(b"o")
        new.write_bytes(b"n")
        import os
        cutoff_ago = (settings.holo_max_age_hours + 1) * 3600
        import time as _time
        os.utime(old, (_time.time() - cutoff_ago, _time.time() - cutoff_ago))
        H._prune_old()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
