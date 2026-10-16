-- Phase 4: recipes (saved prompts that refresh a Plex playlist on a schedule) and the admin user.

CREATE TABLE IF NOT EXISTS recipes (
  id                 INTEGER PRIMARY KEY,
  name               TEXT NOT NULL UNIQUE,
  prompt             TEXT NOT NULL,
  provider_id        INTEGER REFERENCES providers(id) ON DELETE SET NULL,  -- NULL = default provider
  mode               TEXT,                                                -- NULL = provider's mode
  constraints_json   TEXT,
  schedule_cron      TEXT,                                                -- NULL/empty = manual only
  enabled            INTEGER NOT NULL DEFAULT 1,
  avoid_repeats      INTEGER NOT NULL DEFAULT 1,  -- keep the previous refresh's tracks out
  playlist_title     TEXT,                        -- NULL = recipe name
  plex_playlist_key  TEXT,                        -- set by the first successful run
  last_run_at        TEXT,
  last_status        TEXT,                        -- 'running' | 'ok' | 'error'
  last_error         TEXT,
  last_generation_id INTEGER,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id                INTEGER PRIMARY KEY,
  username          TEXT NOT NULL UNIQUE,
  password_hash     TEXT NOT NULL,
  password_from_env INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL
);
