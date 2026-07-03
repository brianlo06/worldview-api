-- 015_game_card_upgrades.sql
--
-- Card upgrades: duplicate copies unlock levels, Flux pays for the upgrade,
-- and higher-level cards generate more passive Flux.

ALTER TABLE game_inventory
    ADD COLUMN IF NOT EXISTS level INT NOT NULL DEFAULT 1;

ALTER TABLE game_inventory
    DROP CONSTRAINT IF EXISTS game_inventory_level_range;

ALTER TABLE game_inventory
    ADD CONSTRAINT game_inventory_level_range CHECK (level >= 1);

INSERT INTO game_rate_tables (module, key, value) VALUES
    ('scan', 'card_upgrades', '{"max_level": 5, "income_bonus_per_level": 0.5, "cost_by_tier": {"common": 20, "uncommon": 45, "rare": 100, "epic": 220, "legendary": 500}}')
ON CONFLICT (module, key) DO NOTHING;
