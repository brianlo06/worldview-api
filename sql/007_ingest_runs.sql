-- 007_ingest_runs.sql
--
-- Per-invocation record of the ingest pipeline runs. One row per call to
-- _run_ingest_subprocess() in api.py — including the case where the lock
-- was held and the run was skipped (skipped_lock_held = true).
--
-- Queried by GET /admin/status to surface "did the last ingest run, when,
-- what was its returncode?" without container-side log access.
--
-- Pruned to 7 days by prune.prune_once().

CREATE TABLE IF NOT EXISTS ingest_runs (
    id                SERIAL      PRIMARY KEY,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    returncode        INTEGER,
    skipped_lock_held BOOLEAN     NOT NULL DEFAULT FALSE,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS ingest_runs_started_at_idx
    ON ingest_runs (started_at DESC);
