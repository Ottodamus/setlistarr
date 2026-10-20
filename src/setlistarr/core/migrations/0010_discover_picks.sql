-- Songs the user picked for the Discover playlist from an arrived artist. When picked_at is set,
-- the playlist uses exactly these (instead of the automatic 3) and never trims them; they stay
-- while the arrivals window counts from picked_at or arrived_at, whichever is later.
ALTER TABLE discoveries ADD COLUMN picked_at TEXT;

CREATE TABLE IF NOT EXISTS discover_picks (
  discovery_id INTEGER NOT NULL REFERENCES discoveries(id) ON DELETE CASCADE,
  plex_key     TEXT NOT NULL,
  position     INTEGER NOT NULL,
  PRIMARY KEY (discovery_id, plex_key)
);
