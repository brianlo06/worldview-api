"""Liveness and token-gated admin endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Response

from ..config import settings
from ..db import get_pool
from ..ingest.orchestrator import _run_ingest_subprocess
from ..observability import KNOWN_SOURCES, read_ingest_log_tail

log = logging.getLogger(__name__)

router = APIRouter()

# Container/process start time — used by /admin/status to report uptime.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


@router.get("/health")
def health(response: Response) -> dict[str, str]:
    """Liveness + DB reachability. Returns 503 if Postgres is unreachable so
    the load balancer / uptime monitor sees a real failure instead of a
    cheerful 200 while every data endpoint is 500ing."""
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        # Deliberately no exception detail in the body — internals stay in logs.
        response.status_code = 503
        return {"status": "degraded", "db": "unreachable"}
    return {"status": "ok", "db": "ok"}


@router.post("/admin/run-ingest")
def admin_run_ingest(
    background_tasks: BackgroundTasks,
    x_admin_token: str = Header(..., alias="X-Admin-Token"),
) -> dict[str, str]:
    """Trigger one ingest pass. Called by the CF cron worker every 15 min.
    Returns 202 immediately; the actual work runs in a background thread.
    Token-gated — must match settings.ingest_token (set as a wrangler secret).
    """
    if not settings.ingest_token:
        # Defensive: refuse to run if the token isn't configured, otherwise
        # an empty token would accept blank-header requests.
        raise HTTPException(status_code=503, detail="ingest disabled")
    if x_admin_token != settings.ingest_token:
        raise HTTPException(status_code=401, detail="invalid token")
    background_tasks.add_task(_run_ingest_subprocess)
    return {"status": "queued"}


@router.get("/admin/status")
def admin_status(
    x_admin_token: str = Header(..., alias="X-Admin-Token"),
) -> dict:
    """Snapshot of ingest pipeline state. Token-gated; same secret as
    /admin/run-ingest. Read-only.

    Returns container uptime + per-source watermarks (with explicit nulls
    for known sources that have never produced) + the last 10 ingest runs
    + the tail of the captured ingest log. Designed to answer "is ingest
    working right now, and if not, where is it failing?" in one request.
    """
    if not settings.ingest_token:
        raise HTTPException(status_code=503, detail="admin status disabled")
    if x_admin_token != settings.ingest_token:
        raise HTTPException(status_code=401, detail="invalid token")

    now = datetime.now(timezone.utc)
    uptime_seconds = int((now - _PROCESS_STARTED_AT).total_seconds())

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source, last_seen_at FROM source_watermarks"
        )
        wm_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, started_at, finished_at, returncode, skipped_lock_held, notes
            FROM ingest_runs
            ORDER BY started_at DESC
            LIMIT 10
            """
        )
        recent_runs_raw = cur.fetchall()

    watermarks: dict[str, dict] = {}
    seen_in_db = {src: ts for src, ts in wm_rows}
    for src in KNOWN_SOURCES:
        ts = seen_in_db.get(src)
        watermarks[src] = {
            "last_seen_at": ts.isoformat() if ts else None,
        }
    # Include any extra sources that DB has but code doesn't list, so a
    # future ingester not yet wired into KNOWN_SOURCES still shows up.
    for src, ts in seen_in_db.items():
        if src not in watermarks:
            watermarks[src] = {"last_seen_at": ts.isoformat() if ts else None}

    def _run_row(r: tuple) -> dict:
        rid, started, finished, rc, skipped, notes = r
        return {
            "id": rid,
            "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "returncode": rc,
            "skipped_lock_held": skipped,
            "notes": notes,
        }

    recent_runs = [_run_row(r) for r in recent_runs_raw]
    last_run = recent_runs[0] if recent_runs else None

    return {
        "container": {
            "uptime_seconds": uptime_seconds,
            "started_at": _PROCESS_STARTED_AT.isoformat(),
        },
        "watermarks": watermarks,
        "last_run": last_run,
        "recent_runs": recent_runs,
        "log_tail": read_ingest_log_tail(max_lines=200),
    }
