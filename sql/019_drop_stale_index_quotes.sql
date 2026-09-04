-- 019_drop_stale_index_quotes.sql
--
-- Stooq retired its /q/l/ quote-CSV endpoint, so all 16 stock indices have
-- been 404ing and their rows sat frozen — prod was serving prices last
-- updated 2026-06-07 as if they were live. The ingester is gone; drop the
-- rows it used to maintain.
--
-- Currency pins written by ingest/currencies.py use "FX:" symbols and are
-- unaffected. Idempotent: re-running deletes nothing once they are gone.

DELETE FROM markets WHERE symbol NOT LIKE 'FX:%';
