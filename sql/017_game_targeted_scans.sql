-- 017_game_targeted_scans.sql
--
-- Targeted scans: a flux-priced scan restricted to a continent or category.
-- Adds the price to the live scan_prices row. Idempotent.

UPDATE game_rate_tables
SET value = value || '{"targeted": 100}'::jsonb
WHERE module = 'scan' AND key = 'scan_prices' AND NOT value ? 'targeted';
