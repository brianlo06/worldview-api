"""Neural TTS synthesis (no real Piper subprocess)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worldview_api import tts as T
from worldview_api.config import settings


def _fake_run_ok(cmd, input=None, capture_output=None, timeout=None):  # noqa: A002
    out = Path(cmd[cmd.index("-f") + 1])
    out.write_bytes(b"RIFFfakewav")
    return mock.Mock(returncode=0, stderr=b"")


class TTSTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(settings, "tts_dir", self._tmp.name),
            mock.patch.object(settings, "tts_enabled", True),
        ]
        for p in self._patches:
            p.start()
        T.budget._reset_for_tests()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_synthesizes_and_caches(self):
        with mock.patch.object(T.subprocess, "run", side_effect=_fake_run_ok) as run:
            p1 = T.synthesize("Good evening.")
            p2 = T.synthesize("Good evening.")
        self.assertIsNotNone(p1)
        self.assertEqual(p1, p2)
        run.assert_called_once()  # second call was a cache hit
        self.assertEqual(p1.read_bytes(), b"RIFFfakewav")

    def test_distinct_texts_distinct_files(self):
        self.assertNotEqual(
            T.wav_path_for("Good evening."), T.wav_path_for("Good morning.")
        )

    def test_whitespace_normalized_into_same_key(self):
        self.assertEqual(
            T.wav_path_for(" ".join("Good   evening. ".split())),
            T.wav_path_for("Good evening."),
        )

    def test_disabled_returns_none(self):
        with mock.patch.object(settings, "tts_enabled", False), \
             mock.patch.object(T.subprocess, "run") as run:
            self.assertIsNone(T.synthesize("hello"))
        run.assert_not_called()

    def test_budget_cap_degrades(self):
        with mock.patch.object(T.budget, "try_acquire", return_value=False), \
             mock.patch.object(T.subprocess, "run") as run:
            self.assertIsNone(T.synthesize("hello"))
        run.assert_not_called()

    def test_piper_failure_returns_none_no_file(self):
        fail = mock.Mock(returncode=1, stderr=b"boom")
        with mock.patch.object(T.subprocess, "run", return_value=fail):
            self.assertIsNone(T.synthesize("hello"))
        self.assertFalse(T.wav_path_for("hello").exists())

    def test_piper_timeout_returns_none(self):
        with mock.patch.object(
            T.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="piper", timeout=1),
        ):
            self.assertIsNone(T.synthesize("hello"))

    def test_text_clamped_to_max_chars(self):
        long = "word " * 400
        with mock.patch.object(T.subprocess, "run", side_effect=_fake_run_ok) as run:
            self.assertIsNotNone(T.synthesize(long))
        sent = run.call_args[1]["input"].decode()
        self.assertLessEqual(len(sent), settings.tts_max_chars)

    def test_schedule_skips_cached_and_blank(self):
        path = T.wav_path_for("already done")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF")
        with mock.patch.object(T.threading, "Thread") as thread:
            T.schedule_synthesis(["already done", "", "   "])
        thread.assert_not_called()

    def test_schedule_warms_new_texts(self):
        with mock.patch.object(T.threading, "Thread") as thread:
            T.schedule_synthesis(["fresh line one", "fresh line two"])
        thread.assert_called_once()
        # Clean up the in-flight registry the (unstarted) worker would clear.
        with T._warm_lock:
            T._warm_inflight.clear()


if __name__ == "__main__":
    unittest.main()
