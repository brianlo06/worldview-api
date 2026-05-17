-- worldview canonical schema (Phase 2: raw events, normalized events, watermarks)
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Raw ingestion landing table — one row per source publication.
CREATE TABLE IF NOT EXISTS raw_events (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  payload       JSONB NOT NULL,
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  processed_at  TIMESTAMPTZ,
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS raw_events_unprocessed_idx
  ON raw_events (processed_at) WHERE processed_at IS NULL;

-- Canonical events served to the frontend.
CREATE TABLE IF NOT EXISTS events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_event_id    UUID REFERENCES raw_events(id),
  title           TEXT NOT NULL,
  summary         TEXT,
  url             TEXT,
  url_hash        TEXT NOT NULL UNIQUE,
  source          TEXT NOT NULL,
  source_outlet   TEXT,
  occurred_at     TIMESTAMPTZ NOT NULL,
  location        GEOGRAPHY(POINT, 4326),
  country_code    CHAR(2),
  region          TEXT,
  city            TEXT,
  categories      TEXT[] NOT NULL DEFAULT '{}',
  sentiment       REAL,
  importance      REAL,
  language        CHAR(2),
  raw             JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS events_location_gix      ON events USING GIST (location);
CREATE INDEX IF NOT EXISTS events_occurred_at_idx   ON events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_country_time_idx  ON events (country_code, occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_categories_gin    ON events USING GIN (categories);

-- Per-source watermark so ingestion can resume from the last seen position.
CREATE TABLE IF NOT EXISTS source_watermarks (
  source       TEXT PRIMARY KEY,
  last_seen_at TIMESTAMPTZ NOT NULL,
  cursor       TEXT,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
