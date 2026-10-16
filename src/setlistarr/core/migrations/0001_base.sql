-- Base schema, carried over unchanged from plex-dj's schema.sql (minus PRAGMAs, which db.connect sets).
-- Every statement is IF NOT EXISTS so an existing plex-dj library.db migrates cleanly.

CREATE TABLE IF NOT EXISTS sync_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS artists (
  plex_key TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  genres   TEXT,   -- json array
  styles   TEXT,   -- json array
  moods    TEXT,   -- json array
  country  TEXT,
  mbid     TEXT
);

CREATE TABLE IF NOT EXISTS albums (
  plex_key   TEXT PRIMARY KEY,
  artist_key TEXT,
  title      TEXT NOT NULL,
  year       INTEGER,
  genres     TEXT,
  styles     TEXT,
  moods      TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
  id             INTEGER PRIMARY KEY,
  plex_key       TEXT UNIQUE NOT NULL,
  title          TEXT NOT NULL,
  artist_key     TEXT,
  artist_name    TEXT,
  album_key      TEXT,
  album_title    TEXT,
  track_num      INTEGER,
  disc_num       INTEGER,
  year           INTEGER,
  duration_ms    INTEGER,
  user_rating    REAL,      -- 0..10 (Plex scale; 5 stars = 10)
  critic_rating  REAL,
  play_count     INTEGER DEFAULT 0,
  skip_count     INTEGER DEFAULT 0,
  last_played_at TEXT,
  added_at       TEXT,
  bpm            REAL,
  musical_key    TEXT,
  loudness       REAL,
  codec          TEXT,
  bitrate        INTEGER,
  file_path      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_key);
CREATE INDEX IF NOT EXISTS idx_tracks_year   ON tracks(year);
CREATE INDEX IF NOT EXISTS idx_tracks_rating ON tracks(user_rating);

CREATE TABLE IF NOT EXISTS track_tags (
  track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  source   TEXT NOT NULL,   -- 'plex-genre' | 'plex-style' | 'plex-mood' | 'lastfm-artist' | 'lastfm-track' | 'musicbrainz'
  tag      TEXT NOT NULL,
  weight   REAL DEFAULT 1.0,
  PRIMARY KEY (track_id, source, tag)
);
CREATE INDEX IF NOT EXISTS idx_track_tags_tag ON track_tags(tag);

CREATE TABLE IF NOT EXISTS artist_similar (
  artist_key   TEXT NOT NULL,
  similar_name TEXT NOT NULL,
  similar_mbid TEXT,
  score        REAL,
  PRIMARY KEY (artist_key, similar_name)
);

CREATE TABLE IF NOT EXISTS enrich_cache (
  cache_key  TEXT PRIMARY KEY,
  fetched_at TEXT NOT NULL,
  payload    TEXT
);
