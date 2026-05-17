-- Phase 2 markets: stocks / index quotes anchored to financial centers.
-- Updated periodically by the markets ingestion worker.
-- Idempotent.

CREATE TABLE IF NOT EXISTS markets (
  symbol        TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  city          TEXT NOT NULL,
  country_code  CHAR(2),
  location      GEOGRAPHY(POINT, 4326) NOT NULL,
  price         NUMERIC,
  prev_close    NUMERIC,
  change_pct    REAL,
  currency      TEXT,
  raw           JSONB,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS markets_location_gix ON markets USING GIST (location);
