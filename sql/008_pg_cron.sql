-- 008_pg_cron.sql
--
-- Database-layer data retention via pg_cron. Replaces the old application
-- prune (prune.py / run_all.py) so retention is enforced inside Postgres on
-- a fixed schedule, independent of the ingest worker.
--
-- Retention window: 3 days for events, raw_events, clusters, anomalies,
-- ingest_runs. markets and region_baselines are intentionally left unbounded
-- (upsert-by-key, fixed size).
--
-- Requires shared_preload_libraries=pg_cron and cron.database_name pointing at
-- this database (set in compose.yaml's db command). Idempotent: safe to apply
-- to an already-initialized database as well as on first boot.

CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Delete rows past the 3-day window in FK-safe order:
--   events.raw_event_id -> raw_events  (no ON DELETE -> RESTRICT): events first
--   events.cluster_id   -> clusters    (ON DELETE SET NULL): clusters anytime
CREATE OR REPLACE FUNCTION prune_old_data()
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    DELETE FROM events       WHERE occurred_at   < now() - interval '3 days';
    DELETE FROM raw_events   WHERE ingested_at   < now() - interval '3 days';
    DELETE FROM clusters     WHERE last_seen     < now() - interval '3 days';
    DELETE FROM anomalies    WHERE last_seen_at  < now() - interval '3 days';
    DELETE FROM ingest_runs  WHERE started_at    < now() - interval '3 days';
END;
$fn$;

-- Schedule hourly. cron.schedule upserts by jobname, so re-applying this file
-- just refreshes the existing job rather than creating duplicates.
SELECT cron.schedule('prune_old_data', '0 * * * *', $job$SELECT prune_old_data();$job$);
