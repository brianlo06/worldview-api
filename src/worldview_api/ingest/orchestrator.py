"""Ingest subprocess orchestration for the /admin/run-ingest endpoint.

Spawns scripts/run_all.py in a child process, records each invocation in the
`ingest_runs` table, and appends the captured output to the rotating ingest
log so /admin/status can surface what happened from outside the container.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..db import get_pool
from ..observability import TIMEOUT_RETURNCODE, append_to_ingest_log

log = logging.getLogger(__name__)

# Module-level lock: prevents a second cron tick from spawning a parallel
# ingest if a previous one is still running (ingest takes ~3 min, cron
# fires every 15 min — usually safe, but guard anyway).
_INGEST_LOCK = threading.Lock()
_RUN_ALL_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_all.py"


def _insert_ingest_run_start(skipped_lock_held: bool) -> int | None:
    """Insert a starting `ingest_runs` row and return its id."""
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_runs (started_at, skipped_lock_held)
                VALUES (NOW(), %s)
                RETURNING id
                """,
                (skipped_lock_held,),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
    except Exception:
        log.exception("ingest: failed to insert ingest_runs row")
        return None


def _update_ingest_run_finish(
    row_id: int | None, returncode: int | None, notes: str | None = None,
) -> None:
    if row_id is None:
        return
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingest_runs
                SET finished_at = NOW(), returncode = %s, notes = %s
                WHERE id = %s
                """,
                (returncode, notes, row_id),
            )
            conn.commit()
    except Exception:
        log.exception("ingest: failed to update ingest_runs row %s", row_id)


def _run_ingest_subprocess() -> None:
    """Spawn run_all.py in a child process. Runs in a Starlette threadpool
    via BackgroundTasks so the response returns immediately. Subprocess
    isolation means the ingest's CPU + memory don't fight the API's event
    loop, and a crash in ingest can't take down the API.

    Records each invocation in the `ingest_runs` table and appends the
    subprocess's stdout+stderr to a rotating log file, so /admin/status
    can surface what happened from outside the container.
    """
    if not _INGEST_LOCK.acquire(blocking=False):
        log.warning("ingest: lock held — another run is in flight, skipping")
        row_id = _insert_ingest_run_start(skipped_lock_held=True)
        _update_ingest_run_finish(row_id, returncode=None, notes="lock held; skipped")
        return

    row_id = _insert_ingest_run_start(skipped_lock_held=False)
    started = datetime.now(timezone.utc)
    try:
        log.info("ingest: starting subprocess (%s)", _RUN_ALL_SCRIPT)
        proc = subprocess.run(
            [sys.executable, str(_RUN_ALL_SCRIPT)],
            check=False,
            timeout=600,  # 10 min hard cap; typical run is ~3 min
            capture_output=True,
            text=True,
        )
        log.info("ingest: subprocess finished rc=%d", proc.returncode)
        header = (
            f"=== ingest run #{row_id} ===\n"
            f"started_at: {started.isoformat()}\n"
            f"finished_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"returncode: {proc.returncode}\n"
        )
        append_to_ingest_log(header, proc.stdout or "", proc.stderr or "")
        _update_ingest_run_finish(row_id, returncode=proc.returncode)
    except subprocess.TimeoutExpired as e:
        log.error("ingest: subprocess timed out after 600s — killed")
        # capture_output buffers the partial output on the exception.
        stdout = (e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        stderr = (e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        header = (
            f"=== ingest run #{row_id} TIMED OUT ===\n"
            f"started_at: {started.isoformat()}\n"
            f"finished_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"returncode: {TIMEOUT_RETURNCODE} (timeout)\n"
        )
        append_to_ingest_log(header, stdout, stderr)
        _update_ingest_run_finish(row_id, returncode=TIMEOUT_RETURNCODE, notes="timed out after 600s")
    except Exception as e:
        log.exception("ingest: subprocess failed")
        header = (
            f"=== ingest run #{row_id} FAILED ===\n"
            f"started_at: {started.isoformat()}\n"
            f"finished_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"error: {type(e).__name__}: {e}\n"
        )
        append_to_ingest_log(header, "", str(e))
        _update_ingest_run_finish(row_id, returncode=None, notes=f"exception: {type(e).__name__}: {e}")
    finally:
        _INGEST_LOCK.release()
