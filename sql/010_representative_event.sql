-- 010_representative_event.sql
--
-- Denormalize each cluster's representative event. Picking the member
-- nearest the centroid (geo-precision first, then has-image, then embedding
-- distance) used to run as a LATERAL per cluster on EVERY /clusters request —
-- ~20s at current data volume because it random-reads embedding vectors for
-- thousands of clusters. The pick now happens once per ingest cycle
-- (cluster/representative.py) and the read path is a plain PK join.
--
-- ON DELETE SET NULL: the pg_cron prune (008) deletes events past 3 days
-- while their cluster can briefly outlive them; a NULL representative just
-- drops the cluster from list responses, same as having no eligible member.
--
-- Idempotent: safe on an already-initialized database.

ALTER TABLE clusters
  ADD COLUMN IF NOT EXISTS representative_event_id UUID
    REFERENCES events(id) ON DELETE SET NULL;
