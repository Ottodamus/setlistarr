-- Phase 1: multi-section sync, incremental sync bookkeeping, settings store, job log.

ALTER TABLE artists ADD COLUMN section_id INTEGER;
ALTER TABLE artists ADD COLUMN plex_updated_at TEXT;
ALTER TABLE albums  ADD COLUMN section_id INTEGER;
ALTER TABLE albums  ADD COLUMN plex_updated_at TEXT;
ALTER TABLE tracks  ADD COLUMN section_id INTEGER;
ALTER TABLE tracks  ADD COLUMN plex_updated_at TEXT;
-- Plex originalTitle: the per-track artist when it differs from the album artist
-- (compilations, features). artist_name stays the album artist (grandparentTitle).
ALTER TABLE tracks  ADD COLUMN track_artist TEXT;
-- Track-level Plex tags (json arrays). Track moods are often the richest mood data Plex has.
ALTER TABLE tracks  ADD COLUMN genres TEXT;
ALTER TABLE tracks  ADD COLUMN moods TEXT;
-- Soft delete: tracks gone from Plex keep their row so history/drafts still render.
ALTER TABLE tracks  ADD COLUMN removed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_tracks_section      ON tracks(section_id);
CREATE INDEX IF NOT EXISTS idx_tracks_removed      ON tracks(removed_at);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_lower ON tracks(lower(artist_name));
CREATE INDEX IF NOT EXISTS idx_tracks_title_lower  ON tracks(lower(title));

CREATE TABLE IF NOT EXISTS libraries (
  section_id        INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 0,
  track_count       INTEGER,
  last_sync_at      TEXT,
  last_full_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,      -- 'sync' | 'sync-full' | 'enrich' | later: 'recipe'
  trigger     TEXT,               -- 'schedule' | 'manual' | 'cli'
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,      -- 'running' | 'ok' | 'error' | 'skipped' | 'interrupted'
  stats_json  TEXT,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at);
