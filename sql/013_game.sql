-- 013_game.sql
--
-- Backing tables for the game spine + SCAN module (game-spine-scan change).
--
-- Retention: game_* tables are PERMANENT player data — they are deliberately
-- absent from prune_old_data() (008_pg_cron.sql) and must stay that way.
-- game_card_pool snapshots every display field from clusters at mint time and
-- holds NO foreign keys into retention-managed tables, so cards stay
-- renderable after their source cluster is pruned (3-day window).
--
-- Idempotent — safe to re-run. On a fresh DB this runs via
-- /docker-entrypoint-initdb.d; on an existing prod DB apply it manually.

-- Anonymous players. token_hash is SHA-256 of a server-minted secret; the
-- plaintext token exists only in the provisioning response.
CREATE TABLE IF NOT EXISTS game_players (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash     TEXT        NOT NULL UNIQUE,
    name           TEXT,
    streak_days    INT         NOT NULL DEFAULT 0,
    last_scan_day  DATE,                            -- UTC day of most recent scan (streak bookkeeping)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT game_players_name_len CHECK (name IS NULL OR char_length(name) <= 40)
);

-- One wallet row per player; scan spends lock this row (SELECT ... FOR UPDATE).
CREATE TABLE IF NOT EXISTS game_wallet (
    player_id         UUID PRIMARY KEY REFERENCES game_players(id) ON DELETE CASCADE,
    flux              INT  NOT NULL DEFAULT 0,
    scans_left        INT  NOT NULL DEFAULT 0,
    scans_granted_day DATE,                         -- UTC day the current allowance was granted
    since_epic        INT  NOT NULL DEFAULT 0,      -- pity counters
    since_legendary   INT  NOT NULL DEFAULT 0,
    income_claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Daily card pool. source_cluster_id is provenance only (plain UUID value, no
-- FK) and uniqueness guard for idempotent minting.
CREATE TABLE IF NOT EXISTS game_card_pool (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    pool_date         DATE        NOT NULL,
    source_cluster_id UUID        NOT NULL,
    tier              TEXT        NOT NULL CHECK (tier IN ('common','uncommon','rare','epic','legendary')),
    weight            REAL        NOT NULL DEFAULT 1,
    headline          TEXT        NOT NULL,
    summary           TEXT,
    lat               REAL,
    lon               REAL,
    country           TEXT,
    category          TEXT,
    importance        REAL,
    image_url         TEXT,
    source_outlet     TEXT,
    has_image         BOOLEAN     NOT NULL DEFAULT FALSE,
    art_seed          BIGINT      NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT game_card_pool_uniq UNIQUE (pool_date, source_cluster_id)
);

CREATE INDEX IF NOT EXISTS game_card_pool_date_tier_idx
    ON game_card_pool (pool_date DESC, tier);

-- Append-only roll audit log. No game code path updates or deletes rows.
CREATE TABLE IF NOT EXISTS game_pulls (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id  UUID        NOT NULL REFERENCES game_players(id) ON DELETE CASCADE,
    module     TEXT        NOT NULL DEFAULT 'scan',
    card_id    UUID        REFERENCES game_card_pool(id),
    tier       TEXT        NOT NULL,
    roll_seed  TEXT        NOT NULL,                -- hex seed material for the roll (audit)
    is_dupe    BOOLEAN     NOT NULL DEFAULT FALSE,
    pity_hit   TEXT,                                -- NULL | 'epic' | 'legendary' (forced by pity)
    pulled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS game_pulls_player_idx
    ON game_pulls (player_id, pulled_at DESC);

CREATE TABLE IF NOT EXISTS game_inventory (
    player_id UUID        NOT NULL REFERENCES game_players(id) ON DELETE CASCADE,
    card_id   UUID        NOT NULL REFERENCES game_card_pool(id),
    module    TEXT        NOT NULL DEFAULT 'scan',
    tier      TEXT        NOT NULL,
    count     INT         NOT NULL DEFAULT 1,
    level     INT         NOT NULL DEFAULT 1 CHECK (level >= 1),
    first_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, card_id)
);

CREATE TABLE IF NOT EXISTS game_badges (
    player_id UUID        NOT NULL REFERENCES game_players(id) ON DELETE CASCADE,
    badge_key TEXT        NOT NULL,
    earned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, badge_key)
);

CREATE TABLE IF NOT EXISTS game_income_log (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id   UUID        NOT NULL REFERENCES game_players(id) ON DELETE CASCADE,
    amount      INT         NOT NULL CHECK (amount >= 0),
    earned_from TEXT        NOT NULL DEFAULT 'cards',
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS game_income_log_player_idx
    ON game_income_log (player_id, claimed_at DESC);

-- Live-tunable configuration: rates, pity, allowances, mint parameters.
-- Changing a row takes effect on the next roll/mint — no deploy.
CREATE TABLE IF NOT EXISTS game_rate_tables (
    module     TEXT        NOT NULL,
    key        TEXT        NOT NULL,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (module, key)
);

-- Grim-content exclusion terms (matched case-insensitively against headline +
-- summary at mint). Data, not code: extend with INSERT, no deploy.
CREATE TABLE IF NOT EXISTS game_grim_terms (
    term     TEXT        PRIMARY KEY,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Rolling 30-day event counts per country, refreshed at mint; drives the
-- country-rarity tier bump.
CREATE TABLE IF NOT EXISTS game_country_freq (
    country    TEXT        PRIMARY KEY,
    events_30d INT         NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Seeds (idempotent)

INSERT INTO game_rate_tables (module, key, value) VALUES
    ('scan', 'tier_weights', '{"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1}'),
    ('scan', 'pity',         '{"epic": 20, "legendary": 90}'),
    ('scan', 'dupe_flux',    '{"common": 5, "uncommon": 15, "rare": 40, "epic": 100, "legendary": 250}'),
    ('scan', 'daily_scans',  '{"base": 3, "streak_min_days": 7, "streak_amount": 4}'),
    ('scan', 'mint',         '{"importance_floor": 0.45, "pool_cap": 120, "sparse_min": 40, "legendary_max": 3}'),
    ('scan', 'card_income',  '{"daily_by_tier": {"common": 1, "uncommon": 2, "rare": 5, "epic": 12, "legendary": 30}, "duplicate_bonus": 0.25, "duplicate_bonus_cap": 4, "freshness_hours": 24, "freshness_multiplier": 2, "accrual_cap_hours": 24}'),
    ('scan', 'scan_prices',  '{"bonus": 60}'),
    ('scan', 'card_upgrades','{"max_level": 5, "income_bonus_per_level": 0.5, "cost_by_tier": {"common": 20, "uncommon": 45, "rare": 100, "epic": 220, "legendary": 500}}')
ON CONFLICT (module, key) DO NOTHING;

INSERT INTO game_grim_terms (term) VALUES
    ('killed'), ('kills'), ('dead'), ('deaths'), ('death toll'), ('dies'),
    ('massacre'), ('genocide'), ('mass shooting'), ('shooting'), ('shot dead'),
    ('bombing'), ('airstrike'), ('air strike'), ('casualties'), ('murdered'),
    ('murder'), ('stabbing'), ('stabbed'), ('terror attack'), ('terrorist'),
    ('hostage'), ('famine'), ('atrocity'), ('executed'), ('execution'),
    ('beheaded'), ('war crime'), ('mass grave'), ('suicide'), ('rape'),
    ('abuse victims'), ('bodies found'), ('found dead'), ('fatal'), ('fatalities')
ON CONFLICT (term) DO NOTHING;
