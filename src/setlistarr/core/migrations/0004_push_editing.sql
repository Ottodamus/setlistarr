-- Phase 3: pushing drafts to Plex, draft editing, regeneration around kept tracks.

-- Tracks the user locked: regenerate keeps them, and the validator never drops or trims them.
ALTER TABLE draft_tracks ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;

-- A regeneration remembers the draft it started from.
ALTER TABLE generations ADD COLUMN parent_draft_id INTEGER;

-- The ONLY Plex playlists Setlistarr will ever modify: ones it created itself.
CREATE TABLE IF NOT EXISTS managed_playlists (
  plex_playlist_key TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  track_count       INTEGER,
  last_draft_id     INTEGER,
  recipe_id         INTEGER,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pushes (
  id                INTEGER PRIMARY KEY,
  draft_id          INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
  plex_playlist_key TEXT,
  title             TEXT,
  action            TEXT NOT NULL,   -- 'created' | 'replaced' | 'recreated' (managed playlist was deleted in Plex)
  track_count       INTEGER,
  skipped_json      TEXT,            -- draft tracks no longer in the library, left out of the push
  pushed_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushes_draft ON pushes(draft_id);
