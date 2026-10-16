-- Phase 2: AI providers, pricing, generations and drafts.

CREATE TABLE IF NOT EXISTS providers (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL CHECK (kind IN ('anthropic', 'openai_compat', 'ollama')),
  base_url     TEXT,
  api_key      TEXT,                 -- blank = fall back to ANTHROPIC_API_KEY / OPENAI_API_KEY
  model        TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '{}',
  tool_mode    TEXT NOT NULL DEFAULT 'auto' CHECK (tool_mode IN ('auto', 'tools', 'two_stage')),
  is_default   INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);

-- USD per million tokens. Models without a row are logged with cost NULL ("no pricing").
CREATE TABLE IF NOT EXISTS model_pricing (
  model                TEXT PRIMARY KEY,
  input_per_mtok       REAL NOT NULL,
  output_per_mtok      REAL NOT NULL,
  cache_read_per_mtok  REAL,
  cache_write_per_mtok REAL,          -- 5-minute TTL write (1.25x input)
  note                 TEXT
);
-- Anthropic first-party list prices as of 2026-09-25. Editable in Settings -> Providers.
INSERT OR IGNORE INTO model_pricing VALUES
  ('claude-opus-5-5',   4.00, 20.00, 0.20, 5.00,  'seeded'),
  ('claude-sonnet-5-5', 2.00, 10.00, 0.20, 2.50,  'seeded'),
  ('claude-haiku-4-5',  1.00,  5.00, 0.10, 1.25,  'seeded'),
  ('claude-fable-5-1', 10.00, 50.00, 0.25, 12.50, 'seeded'),
  ('claude-opus-5',     5.00, 25.00, 0.50, 6.25,  'seeded'),
  ('claude-sonnet-5',   2.00, 10.00, 0.20, 2.50,  'seeded');

CREATE TABLE IF NOT EXISTS generations (
  id                 INTEGER PRIMARY KEY,
  created_at         TEXT NOT NULL,
  finished_at        TEXT,
  prompt             TEXT NOT NULL,
  constraints_json   TEXT,
  provider_id        INTEGER REFERENCES providers(id) ON DELETE SET NULL,
  provider_name      TEXT,
  model              TEXT,
  mode_requested     TEXT,
  mode_used          TEXT,            -- 'tools' | 'two_stage'
  status             TEXT NOT NULL,   -- 'running' | 'ok' | 'error' | 'cancelled' | 'interrupted'
  title              TEXT,
  reasoning          TEXT,
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd           REAL,
  llm_calls          INTEGER NOT NULL DEFAULT 0,
  duration_ms        INTEGER,
  error              TEXT,
  trace_json         TEXT,            -- progress events, for the history view
  recipe_id          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_generations_created ON generations(created_at);

CREATE TABLE IF NOT EXISTS drafts (
  id               INTEGER PRIMARY KEY,
  generation_id    INTEGER REFERENCES generations(id) ON DELETE CASCADE,
  title            TEXT,
  description      TEXT,
  status           TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'pushed' | 'discarded'
  adjustments_json TEXT,                           -- what the validator changed, for the UI
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_tracks (
  draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  plex_key TEXT NOT NULL,
  source   TEXT NOT NULL DEFAULT 'model',  -- 'model' | 'backfill' | 'user'
  PRIMARY KEY (draft_id, position)
);
