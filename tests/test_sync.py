import pytest

from setlistarr.core.plex.client import PlexClient, PlexError
from setlistarr.core.plex.sync import refresh_sections, sync_all, sync_section


def _tags(conn, plex_key):
    return {
        (r["source"], r["tag"])
        for r in conn.execute(
            "SELECT source, tag FROM track_tags tt JOIN tracks t ON t.id = tt.track_id WHERE t.plex_key = ?",
            (plex_key,),
        )
    }


def test_refresh_sections_only_music_and_autoenables_single(conn, plex):
    libs = refresh_sections(conn, plex)
    assert [(l["section_id"], l["name"], l["enabled"]) for l in libs] == [(1, "Music", 1)]


def test_full_sync(conn, plex, fake_plex):
    out = sync_all(conn, plex, full=True)
    st = out["Music"]
    assert st["mode"] == "full" and st["tracks"] == 6 and st["live_tracks"] == 6

    t = conn.execute("SELECT * FROM tracks WHERE plex_key = '100'").fetchone()
    assert (t["title"], t["artist_name"], t["album_title"], t["year"]) == ("Alison", "Slowdive", "Souvlaki", 1993)
    assert t["play_count"] == 3 and t["user_rating"] == 10 and t["section_id"] == 1
    assert t["file_path"] == "/music/100.flac"
    # artist genre/style/mood + album style + track-level mood (only in full metadata)
    assert {("plex-genre", "rock"), ("plex-style", "shoegaze"), ("plex-style", "dream pop"),
            ("plex-mood", "dreamy"), ("plex-mood", "melancholy")} <= _tags(conn, "100")

    comp = conn.execute("SELECT artist_name, track_artist FROM tracks WHERE plex_key = '105'").fetchone()
    assert (comp["artist_name"], comp["track_artist"]) == ("Pixies", "Pixies feat. Guest")

    # Listing pages + batched metadata - no per-item requests.
    assert len(fake_plex.requests_matching(r"/library/metadata/")) == 3  # artists, albums, tracks


def test_incremental_picks_up_edits_and_plays_only(conn, plex, fake_plex):
    sync_all(conn, plex, full=True)
    last = conn.execute("SELECT last_sync_at FROM libraries").fetchone()[0]
    from datetime import datetime

    after = int(datetime.fromisoformat(last).timestamp()) + 10
    fake_plex.items["102"]["title"] = "Debaser (Remaster)"
    fake_plex.items["102"]["updatedAt"] = after
    fake_plex.play("104", at=after)
    fake_plex.log.clear()

    st = sync_section(conn, plex, 1)
    assert st["mode"] == "incremental" and st["tracks"] == 2
    assert conn.execute("SELECT title FROM tracks WHERE plex_key='102'").fetchone()[0] == "Debaser (Remaster)"
    assert conn.execute("SELECT play_count FROM tracks WHERE plex_key='104'").fetchone()[0] == 1
    # only the two changed tracks went through the metadata pass
    assert fake_plex.requests_matching(r"/library/metadata/10[24],10[24]")


def test_incremental_escalates_to_full_on_deletion(conn, plex, fake_plex):
    sync_all(conn, plex, full=True)
    del fake_plex.items["103"]

    st = sync_section(conn, plex, 1)
    assert st["escalated_from_incremental"] and st["removed"] == 1 and st["live_tracks"] == 5
    assert conn.execute("SELECT removed_at FROM tracks WHERE plex_key='103'").fetchone()[0] is not None

    # and it comes back if Plex re-adds it
    fake_plex.add_track(103, 11, "Hey", index=2)
    sync_section(conn, plex, 1, full=True)
    assert conn.execute("SELECT removed_at FROM tracks WHERE plex_key='103'").fetchone()[0] is None


def test_disabled_section_is_not_synced(conn, plex, fake_plex):
    fake_plex.sections.append({"key": "3", "title": "Audiobooks", "type": "artist"})
    refresh_sections(conn, plex)
    conn.execute("UPDATE libraries SET enabled = (section_id = 1)")
    conn.commit()
    assert list(sync_all(conn, plex, full=True)) == ["Music"]


def test_plex_errors_are_friendly(fake_plex):
    with PlexClient(fake_plex.url, "wrong") as bad, pytest.raises(PlexError, match="rejected the token"):
        bad.identity()
    with PlexClient("http://127.0.0.1:9", "x", timeout=1) as down, pytest.raises(PlexError, match="unreachable"):
        down.identity()
