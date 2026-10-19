-- Playlist covers: uploaded (cropped) images and AI-generated ones.
-- Image files live in /config/covers/<file>; rows only hold metadata.

CREATE TABLE IF NOT EXISTS covers (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('upload', 'ai_image', 'ai_art')),
  file        TEXT NOT NULL UNIQUE,     -- file name inside /config/covers
  draft_id    INTEGER REFERENCES drafts(id) ON DELETE SET NULL,   -- where it was made (for "previous covers")
  recipe_id   INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
  prompt      TEXT,                     -- AI covers: the prompt actually sent
  provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
  model       TEXT,
  cost_usd    REAL,                     -- NULL = unknown (no pricing row); 0 = free/local
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_covers_draft ON covers(draft_id);

ALTER TABLE drafts ADD COLUMN cover_id INTEGER REFERENCES covers(id) ON DELETE SET NULL;

-- Recipes: 'none' = leave the Plex cover alone, 'keep' = always use cover_id,
-- 'renew' = generate a fresh AI cover on every successful run.
ALTER TABLE recipes ADD COLUMN cover_mode TEXT NOT NULL DEFAULT 'none';
ALTER TABLE recipes ADD COLUMN cover_id INTEGER REFERENCES covers(id) ON DELETE SET NULL;
ALTER TABLE recipes ADD COLUMN cover_prompt TEXT;      -- NULL = build one from the playlist title/description
ALTER TABLE recipes ADD COLUMN cover_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL;

-- Which cover is currently on the Plex playlist, so an unchanged cover isn't re-uploaded.
ALTER TABLE managed_playlists ADD COLUMN cover_id INTEGER;

-- Current image models; token prices as published by OpenAI (2026-10). Editable in the UI.
INSERT OR IGNORE INTO model_pricing VALUES
  ('gpt-image-2.5-flare',    5.00, 30.00, 1.25, NULL, 'seeded: image output tokens'),
  ('gpt-image-2.5-sunburst', 5.00, 30.00, 1.25, NULL, 'seeded: image output tokens');
