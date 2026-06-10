-- 009_shares_ask_cache.sql
--
-- Backing tables for the viral-share-loop change:
--   * shares    — server-rendered share snapshots behind /s/<id> (OG card +
--                 deep-link redirect). Card fields are denormalized at create
--                 time so a share stays valid after its source cluster ages out.
--   * ask_cache — cached / pre-baked answers for POST /ask, keyed by a
--                 normalized form of the question. Keeps interactive traffic
--                 off the free-tier LLM and the uncached pgvector search.
--
-- Idempotent — safe to re-run. On a fresh DB this runs via
-- /docker-entrypoint-initdb.d; on an existing prod DB apply it manually
-- (psql) since initdb only fires on an empty data dir.

CREATE TABLE IF NOT EXISTS shares (
    id          TEXT        PRIMARY KEY,            -- short random base62 slug
    kind        TEXT        NOT NULL,               -- 'ask' | 'city' | 'cluster' | 'view'
    params      JSONB       NOT NULL DEFAULT '{}',  -- deep-link params to rehydrate the SPA
    -- Denormalized card snapshot (rendered into the PNG + OG meta):
    title       TEXT,                               -- headline / question echo
    place       TEXT,                               -- resolved place label (city-centroid)
    question    TEXT,                               -- sanitized, length-capped user question
    answer      TEXT,                               -- JARVIS answer text
    fly_lat     REAL,
    fly_lon     REAL,
    stats       JSONB       NOT NULL DEFAULT '{}',  -- {event_count, sources, sigma, ...}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS shares_created_at_idx
    ON shares (created_at DESC);

CREATE TABLE IF NOT EXISTS ask_cache (
    normalized_key  TEXT        PRIMARY KEY,        -- normalized question / intent + coord bucket
    question        TEXT,                           -- a representative raw question
    answer          TEXT        NOT NULL,
    place           TEXT,
    fly_lat         REAL,
    fly_lon         REAL,
    cluster_refs    UUID[]      NOT NULL DEFAULT '{}',
    stats           JSONB       NOT NULL DEFAULT '{}',
    source          TEXT        NOT NULL DEFAULT 'live',  -- 'live' | 'degraded' | 'prebaked'
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Freshness lookups filter on computed_at; prebaked rows are refreshed each
-- ingest cycle so they stay warm regardless of the interactive TTL.
CREATE INDEX IF NOT EXISTS ask_cache_computed_at_idx
    ON ask_cache (computed_at DESC);
