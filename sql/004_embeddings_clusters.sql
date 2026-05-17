-- Phase 3: embeddings + clusters.
-- 384 dims matches fastembed's BAAI/bge-small-en-v1.5 model.
-- Idempotent.

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS embedding  vector(384),
  ADD COLUMN IF NOT EXISTS cluster_id UUID;

CREATE TABLE IF NOT EXISTS clusters (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title                TEXT NOT NULL,
  summary              TEXT,
  first_seen           TIMESTAMPTZ NOT NULL,
  last_seen            TIMESTAMPTZ NOT NULL,
  event_count          INT NOT NULL DEFAULT 0,
  centroid_embedding   vector(384) NOT NULL,
  centroid_location    GEOGRAPHY(POINT, 4326),
  primary_country      CHAR(2),
  primary_category     TEXT,
  importance_score     REAL,
  summarized_at        TIMESTAMPTZ,
  summarized_at_count  INT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Add the FK only if absent (so the migration is idempotent)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'events_cluster_fk'
  ) THEN
    ALTER TABLE events
      ADD CONSTRAINT events_cluster_fk
        FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE SET NULL;
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS events_embedding_hnsw
  ON events USING HNSW (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS events_cluster_idx
  ON events (cluster_id);

CREATE INDEX IF NOT EXISTS clusters_last_seen_idx
  ON clusters (last_seen DESC);
CREATE INDEX IF NOT EXISTS clusters_centroid_loc_gix
  ON clusters USING GIST (centroid_location);
CREATE INDEX IF NOT EXISTS clusters_centroid_embedding_hnsw
  ON clusters USING HNSW (centroid_embedding vector_cosine_ops);
