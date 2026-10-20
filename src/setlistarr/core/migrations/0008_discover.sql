-- Discover: recommended artists that are NOT in the library, optionally sent to Lidarr, and a
-- rolling Discover Weekly/Monthly playlist built from what arrives.

CREATE TABLE IF NOT EXISTS discoveries (
  id                INTEGER PRIMARY KEY,
  ident             TEXT NOT NULL UNIQUE,   -- artist MBID, or 'name:<normalized name>' if unknown
  artist_name       TEXT NOT NULL,
  artist_mbid       TEXT,
  source            TEXT NOT NULL,          -- 'lastfm' | 'listenbrainz'
  score             REAL NOT NULL DEFAULT 0,
  reasons_json      TEXT,                   -- ["Slowdive", "Ride"] or recommended track names
  status            TEXT NOT NULL DEFAULT 'suggested',
                    -- suggested | requested | arrived | dismissed | failed
  album_title       TEXT,                   -- album sent to Lidarr
  album_mbid        TEXT,                   -- MusicBrainz release-group id (Lidarr foreignAlbumId)
  lidarr_artist_id  INTEGER,
  note              TEXT,                   -- e.g. "already in Lidarr", why it failed
  first_seen_at     TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  requested_at      TEXT,
  arrived_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_discoveries_status ON discoveries(status);

CREATE TABLE IF NOT EXISTS discover_runs (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  trigger     TEXT,
  status      TEXT NOT NULL,              -- running | ok | error
  stats_json  TEXT,
  error       TEXT
);
