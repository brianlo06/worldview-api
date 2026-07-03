-- 016_game_card_images.sql
--
-- Persistent images for game cards. image_url/source_outlet are snapshotted
-- from the representative event; has_image reflects a successfully cached
-- local thumbnail served by GET /game/cards/{id}/image.

ALTER TABLE game_card_pool
    ADD COLUMN IF NOT EXISTS image_url TEXT,
    ADD COLUMN IF NOT EXISTS source_outlet TEXT,
    ADD COLUMN IF NOT EXISTS has_image BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS game_card_pool_has_image_idx
    ON game_card_pool (has_image)
    WHERE has_image;
