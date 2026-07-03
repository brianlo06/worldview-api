-- World Notes: public pin-a-note globe.
-- Apply: docker exec -i worldview-db-1 psql -U <user> -d <db> < sql/012_world_notes.sql
-- Stop ingest first if ALTER TABLE ever needs to follow this (no ALTER here, safe to run live).

BEGIN;

CREATE TABLE world_notes (
  id            BIGSERIAL    PRIMARY KEY,
  content       TEXT         NOT NULL,
  author_name   TEXT,
  country_code  CHAR(2),
  country_name  TEXT,
  region        TEXT,
  city          TEXT,
  lat           DOUBLE PRECISION NOT NULL DEFAULT 0,
  lng           DOUBLE PRECISION NOT NULL DEFAULT 0,
  ip_hash       TEXT         NOT NULL,
  hearts        INT          NOT NULL DEFAULT 0,
  celebrations  INT          NOT NULL DEFAULT 0,
  prayers       INT          NOT NULL DEFAULT 0,
  waves         INT          NOT NULL DEFAULT 0,
  flagged       BOOLEAN      NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '30 days',

  CONSTRAINT world_notes_content_len CHECK (char_length(content) BETWEEN 1 AND 200),
  CONSTRAINT world_notes_author_len  CHECK (author_name IS NULL OR char_length(author_name) <= 40)
);

CREATE INDEX world_notes_created_idx ON world_notes (created_at DESC);
CREATE INDEX world_notes_expires_idx ON world_notes (expires_at);

-- Per-IP post rate limit (one row per hashed IP, upserted on each post)
CREATE TABLE notepad_rate_limit (
  ip_hash      TEXT        PRIMARY KEY,
  last_post_at TIMESTAMPTZ NOT NULL
);

COMMIT;
