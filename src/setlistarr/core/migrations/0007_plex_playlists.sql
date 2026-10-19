-- Read-only mirror of the Plex user's audio playlists (all of them, not just Setlistarr's),
-- so the library can show which playlists each track / artist is in.

CREATE TABLE IF NOT EXISTS plex_playlists (
  rating_key  TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  smart       INTEGER NOT NULL DEFAULT 0,
  track_count INTEGER,
  updated_at  TEXT,      -- Plex's updatedAt; unchanged playlists aren't re-read
  synced_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_key TEXT NOT NULL REFERENCES plex_playlists(rating_key) ON DELETE CASCADE,
  position     INTEGER NOT NULL,
  plex_key     TEXT NOT NULL,
  PRIMARY KEY (playlist_key, position)
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track ON playlist_tracks(plex_key);
