-- 014_game_passive_income.sql
--
-- Fast-follow economy loop: owned cards generate passive Flux, and Flux can
-- fund bonus scans. Idempotent and additive.

ALTER TABLE game_wallet
    ADD COLUMN IF NOT EXISTS income_claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

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

INSERT INTO game_rate_tables (module, key, value) VALUES
    ('scan', 'card_income', '{"daily_by_tier": {"common": 1, "uncommon": 2, "rare": 5, "epic": 12, "legendary": 30}, "duplicate_bonus": 0.25, "duplicate_bonus_cap": 4, "freshness_hours": 24, "freshness_multiplier": 2, "accrual_cap_hours": 24}'),
    ('scan', 'scan_prices', '{"bonus": 60}'),
    ('scan', 'card_upgrades', '{"max_level": 5, "income_bonus_per_level": 0.5, "cost_by_tier": {"common": 20, "uncommon": 45, "rare": 100, "epic": 220, "legendary": 500}}')
ON CONFLICT (module, key) DO NOTHING;
