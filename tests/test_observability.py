"""Tests for the ingest observability module.

Run: `cd worldview-api && PYTHONPATH=src .venv/bin/python -m unittest tests.test_observability`
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worldview_api import observability


class RedactTests(unittest.TestCase):
    def test_postgres_url_credentials_redacted(self):
        line = "psycopg.OperationalError: could not connect to postgresql://myuser:hunter2@db.host.io:5432/prod"
        out = observability.redact(line)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("myuser", out)
        self.assertIn("[REDACTED]", out)
        # Preserves the rest of the line for context.
        self.assertIn("db.host.io", out)
        self.assertIn("could not connect", out)

    def test_postgres_scheme_no_password_unchanged(self):
        # A bare postgresql:// URL without embedded creds should not be touched.
        line = "connecting to postgresql://db.host.io/prod"
        out = observability.redact(line)
        self.assertEqual(out, line)

    def test_anthropic_key_redacted(self):
        line = "anthropic.AuthError: invalid api key sk-ant-api03-abc123_DEF-xyz received"
        out = observability.redact(line)
        self.assertNotIn("api03-abc123_DEF-xyz", out)
        self.assertIn("sk-ant-[REDACTED]", out)
        self.assertIn("invalid api key", out)

    def test_nvidia_key_redacted(self):
        line = "openai.AuthenticationError: invalid key nvapi-abc123_DEF-xyz received"
        out = observability.redact(line)
        self.assertNotIn("abc123_DEF-xyz", out)
        self.assertIn("nvapi-[REDACTED]", out)
        self.assertIn("invalid key", out)

    def test_env_var_assignment_redacted(self):
        for var in ["DATABASE_URL", "LLM_API_KEY", "ANTHROPIC_API_KEY", "INGEST_TOKEN"]:
            with self.subTest(var=var):
                line = f"setting {var}=some-secret-value"
                out = observability.redact(line)
                self.assertNotIn("some-secret-value", out)
                self.assertIn(f"{var}=[REDACTED]", out)

    def test_innocuous_line_unchanged(self):
        line = "INFO run_all :: === GDELT events ==="
        self.assertEqual(observability.redact(line), line)


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = mock.patch.object(
            observability, "INGEST_LOG_PATH",
            Path(self._tmpdir.name) / "ingest.log",
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_append_writes_header_stdout_stderr(self):
        observability.append_to_ingest_log("=== run #1 ===", "hello stdout", "warn stderr")
        content = observability.INGEST_LOG_PATH.read_text()
        self.assertIn("run #1", content)
        self.assertIn("--- stdout ---", content)
        self.assertIn("hello stdout", content)
        self.assertIn("--- stderr ---", content)
        self.assertIn("warn stderr", content)

    def test_redaction_applied_in_log(self):
        observability.append_to_ingest_log(
            "=== run #1 ===",
            "Connecting postgresql://u:p@h/d",
            "sk-ant-api03-leaked-key",
        )
        content = observability.INGEST_LOG_PATH.read_text()
        self.assertNotIn("u:p", content)
        self.assertNotIn("leaked-key", content)
        self.assertIn("[REDACTED]", content)

    def test_rotation_truncates_to_keep_bytes(self):
        # Write enough content to exceed MAX_BYTES and force a rotation.
        big = "x" * 1024  # 1KB per line
        # 3000 KB total → exceeds 2 MB cap
        for i in range(3000):
            observability.append_to_ingest_log(f"=== run {i} ===", big, "")
        size = observability.INGEST_LOG_PATH.stat().st_size
        # After rotation, file should be at most MAX_BYTES (some buffer for
        # the most recent run that was appended after the last rotation).
        self.assertLessEqual(size, observability.INGEST_LOG_MAX_BYTES + 100_000)
        # The "log rotated" marker should appear, confirming rotation ran.
        content = observability.INGEST_LOG_PATH.read_text()
        self.assertIn("log rotated", content)

    def test_read_tail_returns_last_lines(self):
        observability.append_to_ingest_log("=== run A ===", "line A1\nline A2", "")
        observability.append_to_ingest_log("=== run B ===", "line B1\nline B2", "")
        tail = observability.read_ingest_log_tail(max_lines=5)
        self.assertTrue(any("line B2" in ln for ln in tail))
        # Recent should be in the tail
        self.assertLessEqual(len(tail), 5)

    def test_read_tail_when_no_file(self):
        # Fresh tmpdir means the file doesn't exist.
        self.assertEqual(observability.read_ingest_log_tail(), [])


class RunIngestSubprocessIntegrationTests(unittest.TestCase):
    """Verifies _run_ingest_subprocess inserts/updates ingest_runs rows and
    calls append_to_ingest_log. Doesn't actually spawn run_all.py.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._log_patch = mock.patch.object(
            observability, "INGEST_LOG_PATH",
            Path(self._tmpdir.name) / "ingest.log",
        )
        self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()
        self._tmpdir.cleanup()

    def test_successful_run_inserts_and_updates_row(self):
        from worldview_api.ingest import orchestrator as api_module
        fake_completed = mock.Mock()
        fake_completed.returncode = 0
        fake_completed.stdout = "all good\n=== GDELT events ===\n"
        fake_completed.stderr = ""
        # Capture the row id inserted at start so we can verify it's the
        # same one updated at finish.
        inserted_ids: list[int | None] = []
        updated: list[tuple] = []

        def fake_insert(skipped_lock_held: bool) -> int:
            inserted_ids.append(42)
            return 42

        def fake_update(row_id, returncode, notes=None):
            updated.append((row_id, returncode, notes))

        with mock.patch.object(api_module, "_insert_ingest_run_start", side_effect=fake_insert), \
             mock.patch.object(api_module, "_update_ingest_run_finish", side_effect=fake_update), \
             mock.patch.object(api_module.subprocess, "run", return_value=fake_completed):
            api_module._run_ingest_subprocess()

        self.assertEqual(inserted_ids, [42])
        self.assertEqual(len(updated), 1)
        row_id, returncode, notes = updated[0]
        self.assertEqual(row_id, 42)
        self.assertEqual(returncode, 0)
        # Log file written
        content = observability.INGEST_LOG_PATH.read_text()
        self.assertIn("run #42", content)
        self.assertIn("=== GDELT events ===", content)

    def test_timeout_records_sentinel_returncode(self):
        from worldview_api.ingest import orchestrator as api_module
        import subprocess
        inserted_ids: list[int] = []
        updated: list[tuple] = []

        def fake_insert(skipped_lock_held: bool) -> int:
            inserted_ids.append(7)
            return 7

        def fake_update(row_id, returncode, notes=None):
            updated.append((row_id, returncode, notes))

        with mock.patch.object(api_module, "_insert_ingest_run_start", side_effect=fake_insert), \
             mock.patch.object(api_module, "_update_ingest_run_finish", side_effect=fake_update), \
             mock.patch.object(
                 api_module.subprocess, "run",
                 side_effect=subprocess.TimeoutExpired(cmd="x", timeout=600, output=b"partial", stderr=b"")
             ):
            api_module._run_ingest_subprocess()

        self.assertEqual(updated[0][1], observability.TIMEOUT_RETURNCODE)
        content = observability.INGEST_LOG_PATH.read_text()
        self.assertIn("TIMED OUT", content)


if __name__ == "__main__":
    unittest.main()
