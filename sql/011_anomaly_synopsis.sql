-- 011_anomaly_synopsis.sql
--
-- One-line JARVIS read of each anomaly ("Event volume out of Russia is five
-- times normal, driven by..."), generated at detection time
-- (analyze/synopsis.py) so serving it is free. NULL until the detector next
-- touches the row; the frontend falls back to driver titles.
--
-- NOTE: stop the ingest container before applying (ALTERs deadlock against a
-- running ingest cycle — see 010's deploy notes).

ALTER TABLE anomalies
  ADD COLUMN IF NOT EXISTS synopsis TEXT;
