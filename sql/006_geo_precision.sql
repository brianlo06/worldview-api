-- 006_geo_precision.sql
--
-- Record how precise the `location` column actually is for each event, so
-- the frontend can visually distinguish "we know this happened in Beijing"
-- from "this article was about the United States and we have a country
-- centroid instead of a real point."
--
-- Values:
--   'point'   — exact lat/lon from the source (NOAA polygons, market venues)
--   'city'    — GDELT location types 3 (US city) or 4 (world city)
--   'state'   — GDELT location types 2 (US state) or 5 (world state)
--   'country' — GDELT location type 1 (country centroid)
--   NULL      — unknown / not yet backfilled

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS geo_precision TEXT;

CREATE INDEX IF NOT EXISTS events_geo_precision_idx
  ON events (geo_precision);
