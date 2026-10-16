from setlistarr.core.plex.client import PlexClient
from setlistarr.core.plex.sync import sync_all
from setlistarr.core.tools import ToolContext, dispatch

from .fakes.fake_plex import TOKEN


def _synced(conn, plex):
    sync_all(conn, plex, full=True)
    return ToolContext(conn=conn)


def test_search_hides_removed_and_disabled(conn, plex):
    ctx = _synced(conn, plex)
    assert dispatch(ctx, "search_tracks", {"limit": 100})["count"] == 6

    conn.execute("UPDATE tracks SET removed_at = '2026-01-01' WHERE plex_key = '100'")
    conn.commit()
    keys = {t["plex_key"] for t in dispatch(ctx, "search_tracks", {"limit": 100})["tracks"]}
    assert "100" not in keys and len(keys) == 5
    assert dispatch(ctx, "get_track", {"plex_key": "100"}) == {"found": False}

    conn.execute("UPDATE libraries SET enabled = 0")
    conn.commit()
    assert dispatch(ctx, "search_tracks", {})["count"] == 0
    assert dispatch(ctx, "list_facets", {})["totals"]["tracks"] == 0


def test_search_by_tag_and_track_artist(conn, plex):
    ctx = _synced(conn, plex)
    shoegaze = dispatch(ctx, "search_tracks", {"any_tags": ["Shoegaze"]})
    assert {t["title"] for t in shoegaze["tracks"]} == {"Alison", "Machine Gun"}

    guest = dispatch(ctx, "search_tracks", {"artists": ["pixies feat. guest"]})
    assert [t["title"] for t in guest["tracks"]] == ["Head On"]
    assert guest["tracks"][0]["artist"] == "Pixies feat. Guest"


def test_preview_flags_unknown_keys(conn, plex):
    ctx = _synced(conn, plex)
    p = dispatch(ctx, "build_playlist_preview", {"plex_keys": ["100", "102", "999999"]})
    assert p["track_count"] == 2 and p["warnings"]["missing_keys"] == ["999999"]


def test_create_playlist_requires_writes_and_drops_invented_keys(conn, plex, fake_plex):
    ctx = _synced(conn, plex)
    assert "error" in dispatch(ctx, "create_plex_playlist", {"title": "x", "plex_keys": ["100"]})

    ctx.allow_writes = True
    ctx.plex_factory = lambda: PlexClient(fake_plex.url, TOKEN)
    res = dispatch(
        ctx, "create_plex_playlist",
        {"title": "Test Mix", "plex_keys": ["102", "made-up", "100"], "description": "hi"},
    )
    assert res["ok"] and res["dropped_unknown_keys"] == ["made-up"]
    pl = fake_plex.playlists[res["ratingKey"]]
    assert pl["_keys"] == ["102", "100"] and pl["summary"] == "hi"


def test_unknown_argument_names_list_valid_ones(conn, plex):
    ctx = _synced(conn, plex)
    res = dispatch(ctx, "search_tracks", {"tags": ["rock"], "limit": 5})
    assert "unknown argument(s) for search_tracks: tags" in res["error"] and "any_tags" in res["error"]
