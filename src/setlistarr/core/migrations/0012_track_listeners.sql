-- Plex's track ratingCount: the Last.fm listener count Plex fetches with its own metadata (what drives
-- "Popular tracks" in Plexamp). NULL when Plex has none. Existing rows fill in on the next full sync.
ALTER TABLE tracks ADD COLUMN listeners INTEGER;
-- Make the next sync a full one so existing tracks get their counts without anyone having to ask.
UPDATE libraries SET last_sync_at = NULL;
