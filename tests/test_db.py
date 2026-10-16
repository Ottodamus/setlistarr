from pathlib import Path

from setlistarr.core.db import connect, latest_version, migrate

PLEX_DJ_SCHEMA = Path(__file__).parents[2] / "plex-dj" / "plex_dj" / "schema.sql"


def test_fresh_db_migrates_to_latest(tmp_path):
    c = connect(tmp_path / "x.db")
    assert migrate(c) == latest_version()
    assert migrate(c) == latest_version()  # idempotent
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tracks)")}
    assert {"section_id", "track_artist", "removed_at", "moods"} <= cols


def test_legacy_plex_dj_db_keeps_its_data(tmp_path):
    legacy_sql = (
        PLEX_DJ_SCHEMA.read_text()
        if PLEX_DJ_SCHEMA.exists()
        else (Path(__file__).parents[1] / "src/setlistarr/core/migrations/0001_base.sql").read_text()
    )
    c = connect(tmp_path / "library.db")
    c.executescript(legacy_sql)
    c.execute("INSERT INTO tracks(plex_key, title, artist_name) VALUES('42', 'Old', 'Someone')")
    c.execute("INSERT INTO track_tags(track_id, source, tag) VALUES(1, 'lastfm-artist', 'indie')")
    c.commit()

    assert migrate(c) == latest_version()
    row = c.execute("SELECT title, removed_at, section_id FROM tracks WHERE plex_key='42'").fetchone()
    assert row["title"] == "Old" and row["removed_at"] is None and row["section_id"] is None
    assert c.execute("SELECT COUNT(*) FROM track_tags").fetchone()[0] == 1
