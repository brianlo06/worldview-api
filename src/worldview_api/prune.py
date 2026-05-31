"""Periodic cleanup: delete events / raw_events / clusters / anomalies older
than their retention windows. Called from run_all.py after each ingest pass
so the database stays bounded indefinitely.

Why this exists:
  Ingest writes ~14k events/day. Supabase free tier caps at 500 MB DB
  (~20k events with embeddings + indexes). Without cleanup, the free tier
  fills in ~1.5 days and writes start failing. The frontend only displays
  events from the last 48 hours, so a 14-day retention is generous overhead.

Retention:
  events / raw_events / clusters  → 14 days
  anomalies                       → 30 days
  markets / region_baselines      → unbounded (upsert-by-key, fixed size)

FK order:
  events.raw_event_id REFERENCES raw_events(id)  (no ON DELETE → RESTRICT)
      → must delete events before raw_events
  events.cluster_id REFERENCES clusters(id) ON DELETE SET NULL
      → safe to delete clusters at any time
"""

from __future__ import annotations

import logging

from .db import get_pool

log = logging.getLogger(__name__)

EVENT_RETENTION_DAYS = 14
ANOMALY_RETENTION_DAYS = 30
INGEST_RUN_RETENTION_DAYS = 7


def prune_once() -> dict:
    """Delete rows past their retention window. Returns counts per table."""
    pool = get_pool()
    counts: dict[str, int] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        # 1. Events first (FK from events → raw_events is RESTRICT).
        cur.execute(
            "DELETE FROM events WHERE occurred_at < NOW() - (%s * INTERVAL '1 day')",
            (EVENT_RETENTION_DAYS,),
        )
        counts["events"] = cur.rowcount

        # 2. Raw events that are now orphaned (no event references left).
        cur.execute(
            "DELETE FROM raw_events WHERE ingested_at < NOW() - (%s * INTERVAL '1 day')",
            (EVENT_RETENTION_DAYS,),
        )
        counts["raw_events"] = cur.rowcount

        # 3. Stale clusters. events.cluster_id is ON DELETE SET NULL, so any
        # still-young events that reference a pruned cluster just lose their
        # cluster link rather than getting deleted.
        cur.execute(
            "DELETE FROM clusters WHERE last_seen < NOW() - (%s * INTERVAL '1 day')",
            (EVENT_RETENTION_DAYS,),
        )
        counts["clusters"] = cur.rowcount

        # 4. Anomalies — separately retained longer because they're sparse
        # and the historical context can be useful.
        cur.execute(
            "DELETE FROM anomalies WHERE last_seen_at < NOW() - (%s * INTERVAL '1 day')",
            (ANOMALY_RETENTION_DAYS,),
        )
        counts["anomalies"] = cur.rowcount

        # 5. ingest_runs — observability/debugging metadata; a week is plenty
        # of history for incident postmortems and keeps the table tiny.
        cur.execute(
            "DELETE FROM ingest_runs WHERE started_at < NOW() - (%s * INTERVAL '1 day')",
            (INGEST_RUN_RETENTION_DAYS,),
        )
        counts["ingest_runs"] = cur.rowcount

        conn.commit()

    log.info("prune: %s", counts)
    return {"status": "ok", **counts}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(prune_once())


if __name__ == "__main__":
    main()
