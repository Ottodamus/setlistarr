-- Pick the album when an artist is suggested (not at approval), so the Discover page can show
-- exactly what Lidarr will download, and approving sends that same album.
ALTER TABLE discoveries ADD COLUMN album_how TEXT;          -- how the album was chosen
ALTER TABLE discoveries ADD COLUMN album_checked_at TEXT;   -- set once the album lookup ran
