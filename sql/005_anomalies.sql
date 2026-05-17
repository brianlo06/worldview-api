-- Phase 5 anomaly detection: per-region baselines + spike events.
-- Idempotent.

CREATE TABLE IF NOT EXISTS region_baselines (
  region_code             CHAR(2) PRIMARY KEY,
  baseline_events_per_hr  REAL NOT NULL,
  std_dev                 REAL NOT NULL,
  sample_hours            INT  NOT NULL,
  computed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS anomalies (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  region_code      CHAR(2) NOT NULL,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at         TIMESTAMPTZ,
  peak_rate        REAL NOT NULL,
  baseline_rate    REAL NOT NULL,
  sigma_above      REAL NOT NULL,
  driver_cluster_ids UUID[] NOT NULL DEFAULT '{}',
  pulse_lat        REAL,
  pulse_lon        REAL,
  status           TEXT NOT NULL DEFAULT 'active'  -- 'active' | 'resolved'
);

CREATE INDEX IF NOT EXISTS anomalies_active_idx
  ON anomalies (region_code, started_at DESC) WHERE status = 'active';

-- Index for fast region-level event count queries used by the detector
CREATE INDEX IF NOT EXISTS events_country_occurred_at_idx
  ON events (country_code, occurred_at DESC) WHERE country_code IS NOT NULL;
