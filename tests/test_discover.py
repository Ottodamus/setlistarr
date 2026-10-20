import json

import pytest
from fastapi.testclient import TestClient
from pytest_httpserver import HTTPServer

from setlistarr.config import Settings
from setlistarr.core import enrich as enrich_mod
from setlistarr.core.db import get_meta
from setlistarr.core.lidarr import LidarrClient
from setlistarr.core.listenbrainz import ListenBrainzClient
from setlistarr.core.plex.client import PlexClient
from setlistarr.core.plex.sync import sync_all
from setlistarr.services import discover as svc
from setlistarr.services import scrobble as scrobble_svc
from setlistarr.web.app import create_app

from .fakes.fake_plex import TOKEN
from .fakes.fake_services import LB_TOKEN, LIDARR_KEY, FakeServices

RIDE, BREEDERS, COCTEAU = "mbid-ride", "mbid-breeders", "mbid-cocteau"


@pytest.fixture
def fs(monkeypatch):
    server = HTTPServer()
    server.start()
    f = FakeServices()
    f.url = f.install(server)
    monkeypatch.setattr(svc, "MB_ROOT", f.url + "/ws/2/")
    monkeypatch.setattr(svc, "LASTFM_ROOT", f.url + "/2.0/")
    monkeypatch.setattr(enrich_mod.time, "sleep", lambda s: None)  # MusicBrainz rate limit
    yield f
    server.clear()
    server.stop()


@pytest.fixture
def lib(conn, plex, configured, fs):
    sync_all(conn, plex, full=True)
    # Last.fm similar-artist data as enrichment stores it (includes artists you DON'T own)
    sim = [("1", "Ride", RIDE, 0.9), ("1", "Cocteau Twins", None, 0.8), ("1", "Pixies", None, 0.5),
           ("2", "Ride", RIDE, 0.4), ("2", "The Breeders", BREEDERS, 0.9)]
    conn.executemany("INSERT INTO artist_similar(artist_key, similar_name, similar_mbid, score) VALUES(?,?,?,?)", sim)
    conn.commit()
    s = Settings(conn)
    for k, v in {"lidarr_url": fs.url, "lidarr_api_key": LIDARR_KEY, "lidarr_root_folder": "/music",
                 "lidarr_quality_profile_id": 2, "lidarr_metadata_profile_id": 1}.items():
        s.set(k, v)
    return conn


def lidarr(fs):
    return LidarrClient(fs.url, LIDARR_KEY)


def lb(fs, token=LB_TOKEN):
    return ListenBrainzClient(token, base_url=fs.url)


# ------------------------------------------------------------------- candidates
def test_lastfm_candidates_rank_outsiders_by_your_taste(lib):
    cands = svc.candidates_lastfm(lib)
    names = [c["name"] for c in cands]
    assert "Pixies" not in names  # owned
    assert names[0] == "Ride"  # pointed at by both of your artists
    ride = cands[0]
    assert ride["mbid"] == RIDE and set(ride["reasons"]) == {"Slowdive", "Pixies"}
    assert {"The Breeders", "Cocteau Twins"} <= set(names)


def test_lastfm_without_data_explains_what_to_do(conn):
    with pytest.raises(svc.DiscoverError, match="Last.fm API key"):
        svc.candidates_lastfm(conn)


def _status(conn, did):
    return conn.execute("SELECT status FROM discoveries WHERE id=?", (did,)).fetchone()[0]


def test_dismiss_lets_it_come_back_later_block_never(lib):
    cands = svc.candidates_lastfm(lib)
    assert svc.refresh(lib, cands, per_run=2) == 2
    ride = lib.execute("SELECT id FROM discoveries WHERE artist_name='Ride'").fetchone()[0]
    svc.dismiss(lib, ride)
    assert svc.refresh(lib, cands, per_run=5) == 1  # the 3rd one; Ride was dismissed just now
    assert _status(lib, ride) == "dismissed"
    lib.execute("UPDATE discoveries SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (ride,))  # a month+ later
    lib.commit()
    assert svc.refresh(lib, cands, per_run=5) == 1 and _status(lib, ride) == "suggested"  # back on the list
    svc.block(lib, ride)
    lib.execute("UPDATE discoveries SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (ride,))
    lib.commit()
    assert svc.refresh(lib, cands, per_run=5) == 0 and _status(lib, ride) == "blocked"  # never comes back
    svc.block(lib, ride, undo=True)  # unblock = forgotten, so it's a new candidate again
    assert svc.refresh(lib, cands, per_run=5) == 1
    assert lib.execute("SELECT status FROM discoveries WHERE artist_name='Ride'").fetchone()[0] == "suggested"


def test_waiting_list_is_capped(lib):
    cands = [{"name": f"Artist {i}", "mbid": None, "score": 100 - i, "reasons": [], "source": "lastfm"} for i in range(40)]
    assert svc.refresh(lib, cands, per_run=8, max_waiting=12) == 8
    assert svc.refresh(lib, cands, per_run=8, max_waiting=12) == 4  # only room for 4 more
    assert svc.refresh(lib, cands, per_run=8, max_waiting=12) == 0  # full: nothing added
    svc.dismiss(lib, lib.execute("SELECT id FROM discoveries WHERE artist_name='Artist 0'").fetchone()[0])
    assert svc.refresh(lib, cands, per_run=8, max_waiting=12) == 1  # a dismissal frees a slot
    # limit lowered: the lowest-scored extras are dismissed (they can come back), the best stay
    svc.refresh(lib, cands, per_run=8, max_waiting=5)
    waiting = [r[0] for r in lib.execute("SELECT artist_name FROM discoveries WHERE status='suggested' ORDER BY score DESC")]
    assert waiting == ["Artist 1", "Artist 2", "Artist 3", "Artist 4", "Artist 5"]


def test_block_migration_keeps_old_dismissals_permanent(tmp_path, monkeypatch):
    from setlistarr.core import db
    conn = db.connect(tmp_path / "old.db")
    every = db._migrations()
    monkeypatch.setattr(db, "_migrations", lambda: [m for m in every if m[0] <= 10])
    db.migrate(conn)  # an install from before Block existed
    monkeypatch.setattr(db, "_migrations", lambda: every)
    migrate = db.migrate
    conn.execute("INSERT INTO discoveries(ident, artist_name, source, status, first_seen_at, updated_at) "
                 "VALUES('x', 'X', 'lastfm', 'dismissed', 'now', 'now')")
    conn.commit()
    migrate(conn)
    assert conn.execute("SELECT status FROM discoveries").fetchone()[0] == "blocked"


def test_listenbrainz_candidates_group_recordings_by_artist(lib, fs):
    fs.lb_recs = [{"recording_mbid": "r1", "score": 2.0}, {"recording_mbid": "r2", "score": 1.0},
                  {"recording_mbid": "r3", "score": 5.0}]
    fs.lb_meta = {"r1": {"recording": {"name": "Vapour Trail"}, "artist": {"artists": [{"artist_mbid": RIDE, "name": "Ride"}]}},
                  "r2": {"recording": {"name": "Leave Them All Behind"}, "artist": {"artists": [{"artist_mbid": RIDE, "name": "Ride"}]}},
                  "r3": {"recording": {"name": "Debaser"}, "artist": {"artists": [{"artist_mbid": "x", "name": "Pixies"}]}}}
    cands = svc.candidates_listenbrainz(lib, lb(fs, token=None), "lbuser")
    assert [(c["name"], c["score"], c["reasons"]) for c in cands] == [("Ride", 3.0, ["Vapour Trail", "Leave Them All Behind"])]
    fs.lb_recs = []
    with pytest.raises(svc.DiscoverError, match="few weeks"):
        svc.candidates_listenbrainz(lib, lb(fs, token=None), "lbuser")


# ---------------------------------------------------------------------- Lidarr
def test_request_adds_artist_with_only_its_most_popular_album(lib, fs):
    fs.add_catalog_artist(RIDE, "Ride")
    fs.lb_popular[RIDE] = [{"release_group_mbid": "rg-nowhere", "release_group": {"name": "Nowhere", "type": "Album"}},
                           {"release_group_mbid": "rg-going", "release_group": {"name": "Going Blank Again", "type": "Album"}}]
    svc.refresh(lib, svc.candidates_lastfm(lib), per_run=5)
    did = lib.execute("SELECT id FROM discoveries WHERE artist_name='Ride'").fetchone()[0]
    [res] = svc.request(lib, [did], lidarr=lidarr(fs), lb=lb(fs))
    assert res["status"] == "requested" and res["album_title"] == "Nowhere"
    body = fs.lidarr_added[0]
    assert body["foreignArtistId"] == RIDE and body["rootFolderPath"] == "/music"
    assert (body["qualityProfileId"], body["metadataProfileId"], body["monitored"]) == (2, 1, True)
    assert body["monitorNewItems"] == "none"  # no future releases
    assert body["addOptions"] == {"monitor": "unknown", "albumsToMonitor": ["rg-nowhere"], "monitored": True,
                                  "searchForMissingAlbums": True}
    row = lib.execute("SELECT * FROM discoveries WHERE id=?", (did,)).fetchone()
    assert row["lidarr_artist_id"] == 100 and row["album_mbid"] == "rg-nowhere" and row["requested_at"]


def test_popular_album_falls_back_to_lastfm_and_musicbrainz_without_lb_token(lib, fs):
    Settings(lib).set("lastfm_api_key", "lfm-key")
    fs.add_catalog_artist(BREEDERS, "The Breeders")
    fs.lastfm_top_albums["the breeders"] = ["Last Splash"]
    fs.mb_release_groups[("last splash", BREEDERS)] = "rg-last-splash"
    svc.refresh(lib, svc.candidates_lastfm(lib), per_run=5)
    did = lib.execute("SELECT id FROM discoveries WHERE artist_name='The Breeders'").fetchone()[0]
    [res] = svc.request(lib, [did], lidarr=lidarr(fs), lb=None)
    assert res["album_mbid"] == "rg-last-splash" and "Last.fm top album" in res["note"]
    assert fs.lidarr_added[0]["addOptions"]["albumsToMonitor"] == ["rg-last-splash"]


def test_request_edge_cases(lib, fs):
    svc.refresh(lib, svc.candidates_lastfm(lib), per_run=5)
    ids = {r["artist_name"]: r["id"] for r in lib.execute("SELECT id, artist_name FROM discoveries")}
    # no MBID and MusicBrainz doesn't know them -> failed, explained
    [cocteau] = svc.request(lib, [ids["Cocteau Twins"]], lidarr=lidarr(fs))
    assert cocteau["status"] == "failed" and "MusicBrainz" in cocteau["note"]
    # Lidarr's metadata doesn't have Ride yet -> failed; once it does, Retry works.
    # No popularity data available -> Lidarr's 'latest' option instead of a specific album.
    [ride] = svc.request(lib, [ids["Ride"]], lidarr=lidarr(fs))
    assert ride["status"] == "failed" and "Lidarr couldn't find" in ride["note"]
    fs.add_catalog_artist(RIDE, "Ride")
    [retry] = svc.request(lib, [ids["Ride"]], lidarr=lidarr(fs))
    assert retry["status"] == "requested" and fs.lidarr_added[-1]["addOptions"]["monitor"] == "latest"
    assert fs.lidarr_added[-1]["addOptions"]["albumsToMonitor"] == []
    # already in Lidarr -> requested without adding again
    fs.lidarr_artists.append({"id": 7, "foreignArtistId": BREEDERS, "artistName": "The Breeders"})
    [b] = svc.request(lib, [ids["The Breeders"]], lidarr=lidarr(fs))
    assert b["status"] == "requested" and b["note"] == "Already in Lidarr" and len(fs.lidarr_added) == 1


def test_nothing_is_sent_without_approval_unless_auto_approve(lib, fs, plex):
    fs.add_catalog_artist(RIDE, "Ride")
    fs.add_catalog_artist(BREEDERS, "The Breeders")
    res = svc.run(lib, "test", build=False, inject={"lidarr": lidarr(fs)})
    assert res["new_suggestions"] == 3 and fs.lidarr_added == [] and "sent_to_lidarr" not in res
    Settings(lib).set("discover_auto_approve", True)
    res = svc.run(lib, "test", build=False, inject={"lidarr": lidarr(fs)})
    assert res["sent_to_lidarr"] == 2 and res["failed"] == 1  # Cocteau Twins can't be identified


# ------------------------------------------------------------ arrival + playlist
def _arrive_ride(lib, fake_plex, plex, fs):
    fs.add_catalog_artist(RIDE, "Ride")
    svc.refresh(lib, svc.candidates_lastfm(lib), per_run=5)
    did = lib.execute("SELECT id FROM discoveries WHERE artist_name='Ride'").fetchone()[0]
    svc.request(lib, [did], lidarr=lidarr(fs))
    # Lidarr downloaded Nowhere; Plex picked it up
    fake_plex.add_artist(3, "Ride", genres=["Rock"], styles=["Shoegaze"])
    fake_plex.add_album(30, 3, "Nowhere", 1990)
    for i, (t, n) in enumerate([("Seagull", 120_000), ("Kaleidoscope", 310_000), ("In a Different Place", None),
                                ("Polar Bear", None)]):
        fake_plex.add_track(300 + i, 30, t, index=i + 1, listeners=n)  # n = Plex ratingCount
    sync_all(lib, plex, full=True)
    return did


def test_arrival_and_rolling_playlist_with_top_up(lib, fake_plex, plex, fs):
    did = _arrive_ride(lib, fake_plex, plex, fs)
    assert svc.check_arrivals(lib) == 1
    assert lib.execute("SELECT status FROM discoveries WHERE id=?", (did,)).fetchone()[0] == "arrived"
    Settings(lib).set("discover_minutes", 20)  # 6 tracks of 200s
    lib.execute("UPDATE tracks SET play_count = 5 WHERE plex_key IN ('100', '102')")  # your favourites
    lib.commit()
    first = svc.build_playlist(lib, plex=PlexClient(fake_plex.url, TOKEN))
    assert first["status"] == "ok" and first["action"] == "created" and first["title"] == "Discover Weekly"
    assert first["new_tracks"] == 3  # max 3 per arrived artist
    keys = fake_plex.playlists[first_key := get_meta(lib, "discover_playlist_key")]["_keys"]
    assert {"300", "301", "302"} <= set(keys)  # Ride, the album Lidarr fetched
    assert "100" not in keys and "102" not in keys  # top-up is never-played tracks only
    assert len(keys) > 3  # topped up from the library
    second = svc.build_playlist(lib, plex=PlexClient(fake_plex.url, TOKEN))
    assert second["action"] == "replaced" and get_meta(lib, "discover_playlist_key") == first_key


def test_picked_songs_go_into_the_playlist_in_place_and_stay(lib, fake_plex, plex, fs):
    did = _arrive_ride(lib, fake_plex, plex, fs)
    svc.check_arrivals(lib)
    Settings(lib).set("discover_minutes", 20)
    px = lambda: PlexClient(fake_plex.url, TOKEN)  # noqa: E731
    svc.build_playlist(lib, plex=px())
    key = get_meta(lib, "discover_playlist_key")
    before = fake_plex.playlists[key]["_keys"]
    others = [k for k in before if not k.startswith("30")]
    # keep Seagull, swap the other two automatic picks for Polar Bear
    res = svc.set_picks(lib, did, ["300", "303", "999"], plex=px())  # 999 isn't Ride's: ignored
    assert res["status"] == "ok" and res["action"] == "replaced" and res["songs"] == 2
    after = fake_plex.playlists[key]["_keys"]
    assert get_meta(lib, "discover_playlist_key") == key  # same playlist, replaced in place
    assert {"300", "303"} <= set(after) and not {"301", "302"} & set(after)
    assert [k for k in after if not k.startswith("30")] == others  # the rest untouched, same order
    # a rebuild keeps exactly your picks, even when the playlist is over length
    Settings(lib).set("discover_minutes", 1)
    svc.build_playlist(lib, plex=px())
    rebuilt = fake_plex.playlists[key]["_keys"]
    assert {"300", "303"} <= set(rebuilt) and not {"301", "302"} & set(rebuilt)
    assert svc.set_picks(lib, did, ["300", "303"], plex=px())["action"] == "unchanged"
    # back to automatic: the first 3 of the album Lidarr fetched
    svc.set_picks(lib, did, None, plex=px())
    assert {"300", "301", "302"} <= set(fake_plex.playlists[key]["_keys"])
    assert lib.execute("SELECT picked_at FROM discoveries WHERE id=?", (did,)).fetchone()[0] is None
    with pytest.raises(svc.DiscoverError):
        svc.set_picks(lib, lib.execute("SELECT id FROM discoveries WHERE status='suggested'").fetchone()[0], ["300"])


def test_pick_songs_from_the_discover_page(client, lib, fake_plex, plex, fs, monkeypatch):
    did = _arrive_ride(lib, fake_plex, plex, fs)
    svc.check_arrivals(lib)
    page = client.get("/discover").text
    assert f'hx-get="/discover/{did}/songs"' in page and "Pick songs" in page
    picker = client.get(f"/discover/{did}/songs").text
    assert "Songs by Ride for Discover Weekly" in picker and "Polar Bear" in picker and "picks up to 3 automatically" in picker
    real = svc.set_picks
    monkeypatch.setattr(svc, "set_picks", lambda c, i, k: real(c, i, k, plex=PlexClient(fake_plex.url, TOKEN)))
    html = client.post(f"/discover/{did}/songs", data={"k": ["301", "303"]}).text
    assert "Discover Weekly created with 2 songs from this artist" in html and "2 songs · your picks" in html
    keys = fake_plex.playlists[get_meta(lib, "discover_playlist_key")]["_keys"]
    assert {"301", "303"} <= set(keys) and "300" not in keys
    assert "Back to automatic" in client.get(f"/discover/{did}/songs").text


def test_title_key_matches_remasters_and_quotes():
    k = svc._title_key
    assert k("Seagull (2001 Remaster)") == k("Seagull - Remastered") == k("seagull") == "seagull"
    assert k("Why’d You Only Call Me") == k("Why'd You Only Call Me")
    assert k("Live Forever") == "liveforever" and k("Polar Bear [Live at Reading]") == "polarbear"


def test_picker_shows_popularity_most_popular_first(client, lib, fake_plex, plex, fs):
    did = _arrive_ride(lib, fake_plex, plex, fs)
    svc.check_arrivals(lib)
    assert lib.execute("SELECT listeners FROM tracks WHERE plex_key='301'").fetchone()[0] == 310_000  # synced
    Settings(lib).set("lastfm_api_key", "k")
    fs.lastfm_top_tracks["ride"] = {"Polar Bear (2001 Remaster)": 45_300, "Seagull": 1}  # Plex's count wins for Seagull
    d = lib.execute("SELECT * FROM discoveries WHERE id=?", (did,)).fetchone()
    tracks = svc.with_popularity(lib, d, svc.artist_tracks(lib, d))
    assert [(t["title"], t["listeners"], t["popularity_source"], t["popularity_pct"]) for t in tracks] == [
        ("Kaleidoscope", 310_000, "Plex", 100), ("Seagull", 120_000, "Plex", 39),
        ("Polar Bear", 45_300, "Last.fm", 15), ("In a Different Place", None, None, 0)]
    html = client.get(f"/discover/{did}/songs").text
    assert "Popularity" in html and "Plays" not in html and "310K" in html and "45K" in html
    assert html.index("Kaleidoscope") < html.index("Seagull") < html.index("Polar Bear") < html.index("In a Different Place")
    assert "310,000 listeners on Last.fm (via Plex)" in html and "No popularity yet" not in html


def test_playlist_title_follows_cadence(lib):
    s = Settings(lib)
    s.set("discover_cadence", "monthly")
    assert svc.playlist_title(s) == "Discover Monthly"
    s.set("discover_playlist_title", "New For Me")
    assert svc.playlist_title(s) == "New For Me"


# ------------------------------------------------------------------- scrobbling
def test_scrobble_sends_only_your_account_once(lib, fake_plex, plex, fs):
    fake_plex.play_history("100", 1_700_000_100)              # you
    fake_plex.play_history("102", 1_700_000_200)              # you
    fake_plex.play_history("105", 1_700_000_300, account_id=2)  # someone else on your server
    assert scrobble_svc.scrobble(lib, plex, lb=lb(fs)) is None  # off by default
    Settings(lib).set("scrobble_enabled", True)
    Settings(lib).set("listenbrainz_token", LB_TOKEN)
    assert scrobble_svc.scrobble(lib, plex, lb=lb(fs)) == {"sent": 2}
    first = fs.lb_submitted[0]
    assert first["listened_at"] == 1_700_000_100
    assert first["track_metadata"]["artist_name"] == "Slowdive" and first["track_metadata"]["track_name"] == "Alison"
    info = first["track_metadata"]["additional_info"]
    assert info["duration_ms"] == 200_000 and info["media_player"] == "Plex" and info["submission_client"] == "Setlistarr"
    assert scrobble_svc.scrobble(lib, plex, lb=lb(fs)) == {"sent": 0}  # watermark: nothing twice
    fake_plex.play_history("104", 1_700_000_400)
    assert scrobble_svc.scrobble(lib, plex, lb=lb(fs)) == {"sent": 1} and len(fs.lb_submitted) == 3


# ---------------------------------------------------------------------- web UI
@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_settings_lidarr_test_loads_folders_and_profiles(client, conn, fs):
    Settings(conn).set("lidarr_url", "")
    html = client.post("/settings/discover/lidarr-test", data={"lidarr_url": fs.url + "/", "lidarr_api_key": LIDARR_KEY}).text
    assert "Connected to Lidarr 3.0.0.0" in html and "/music" in html and "Lossless" in html and "Standard" in html
    assert Settings(conn).get("lidarr_url") == fs.url  # saved, trailing slash stripped
    bad = client.post("/settings/discover/lidarr-test", data={"lidarr_url": fs.url, "lidarr_api_key": "nope"}).text
    assert "rejected the API key" in bad


def test_discover_page_and_approve_flow(client, lib, fs):
    fs.add_catalog_artist(RIDE, "Ride")
    svc.refresh(lib, svc.candidates_lastfm(lib), per_run=5)
    page = client.get("/discover").text
    assert "Waiting for your OK" in page and "Because you play" in page and "Ride" in page
    did = lib.execute("SELECT id FROM discoveries WHERE artist_name='Ride'").fetchone()[0]
    html = client.post(f"/discover/{did}/approve").text
    assert "Sent Ride to Lidarr" in html and len(fs.lidarr_added) == 1
    settings = client.get("/settings/discover").text
    assert "How the two sources differ" in settings and "Send my Plex plays to ListenBrainz" in settings
    assert "owner (server owner)" in settings  # Plex accounts listed
    client.post("/settings/discover", data={"discover_source": "listenbrainz", "discover_cadence": "weekly",
                                            "discover_per_run": "5", "discover_minutes": "60", "discover_window_weeks": "4",
                                            "discover_cover_mode": "none", "scrobble_plex_account_id": "1"})
    s = Settings(lib)
    assert s.get("discover_source") == "listenbrainz" and s.get("discover_auto_approve") is False
    assert "periodic-discover" in {j.id for j in client.app.state.scheduler._sched.get_jobs()}


def test_album_is_picked_up_front_shown_and_sent_as_shown(client, lib, fs, monkeypatch):
    fs.add_catalog_artist(RIDE, "Ride")
    fs.lb_popular[RIDE] = [{"release_group_mbid": "rg-nowhere", "release_group": {"name": "Nowhere", "type": "Album"}}]
    res = svc.run(lib, "test", build=False, inject={"lb": lb(fs), "lidarr": lidarr(fs)})
    assert res["albums_picked"] >= 1 and fs.lidarr_added == []  # nothing sent: ask-first default
    row = lib.execute("SELECT * FROM discoveries WHERE artist_name='Ride'").fetchone()
    assert (row["album_title"], row["album_mbid"], row["album_how"]) == ("Nowhere", "rg-nowhere", "ListenBrainz listen counts")
    html = client.get("/discover").text
    assert "Album to download" in html and "<b>Nowhere</b>" in html and "musicbrainz.org/release-group/rg-nowhere" in html
    # ListenBrainz's popularity changes later - approving still sends what the page showed
    fs.lb_popular[RIDE] = [{"release_group_mbid": "rg-other", "release_group": {"name": "Other", "type": "Album"}}]
    popularity_calls = len(fs.requests_matching("top-release-groups"))
    [sent] = svc.request(lib, [row["id"]], lidarr=lidarr(fs), lb=lb(fs))
    assert fs.lidarr_added[0]["addOptions"]["albumsToMonitor"] == ["rg-nowhere"] and sent["album_title"] == "Nowhere"
    assert len(fs.requests_matching("top-release-groups")) == popularity_calls  # not looked up again


def test_unidentified_artist_is_flagged_before_approval(lib, fs):
    svc.refresh(lib, svc.candidates_lastfm(lib), per_run=5)
    svc.resolve_albums(lib, None)
    row = lib.execute("SELECT * FROM discoveries WHERE artist_name='Cocteau Twins'").fetchone()
    assert row["album_checked_at"] and row["album_title"] is None and "not found on MusicBrainz" in row["album_how"]


def test_waiting_list_pages_of_8_and_block_buttons(client, lib):
    cands = [{"name": f"Artist {i:02d}", "mbid": None, "score": 100 - i, "reasons": ["ABBA"], "source": "lastfm"}
             for i in range(40)]
    svc.refresh(lib, cands, per_run=40, max_waiting=32)
    page1 = client.get("/discover").text
    assert "Artist 00" in page1 and "Artist 07" in page1 and "Artist 08" not in page1
    assert "32 of 32" in page1 and "The list is full" in page1 and 'hx-get="/discover/lists?page=4"' in page1
    page4 = client.get("/discover/lists?page=4").text
    assert "Artist 24" in page4 and "Artist 31" in page4 and "Artist 23" not in page4
    assert client.get("/discover/lists?page=9").text.count("Artist ") >= 8  # clamped to the last page
    did = lib.execute("SELECT id FROM discoveries WHERE artist_name='Artist 25'").fetchone()[0]
    html = client.post(f"/discover/{did}/block?page=4").text
    assert "Blocked" in html and "Unblock" in html and 'aria-current="page">4' in html  # stays on page 4
    html = client.post(f"/discover/{did}/unblock?page=4").text
    assert lib.execute("SELECT COUNT(*) FROM discoveries WHERE id=?", (did,)).fetchone()[0] == 0


def test_discover_sync_now_button(client, lib, monkeypatch):
    kinds = []
    monkeypatch.setattr(client.app.state.scheduler, "trigger", kinds.append)
    r = client.post("/discover/sync", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/discover?sync=started" and kinds == ["sync"]
    page = client.get("/discover?sync=started").text
    assert "Library sync started" in page and 'action="/discover/sync"' in page
