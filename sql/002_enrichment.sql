-- Phase 2 enrichment: scraped headline metadata + image.
-- Idempotent.

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS image_url      TEXT,
  ADD COLUMN IF NOT EXISTS scraped_at     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS scrape_status  TEXT;

CREATE INDEX IF NOT EXISTS events_unscraped_idx
  ON events (occurred_at DESC) WHERE scraped_at IS NULL;
