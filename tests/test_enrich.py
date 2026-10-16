import json

from werkzeug.wrappers import Response

from setlistarr.core import enrich as enrich_mod
from setlistarr.core.enrich import EnrichConfig, enrich
from setlistarr.core.plex.sync import sync_all

CFG = EnrichConfig(lastfm_api_key=None, musicbrainz_contact="test@example.com", track_tags=False)


class FakeMB:
    def __init__(self):
        self.calls = []
        self.fail_next = 0

    def __call__(self, req):
        self.calls.append(req.full_path)
        if self.fail_next:
            self.fail_next -= 1
            return Response("slow down", 503, headers={"Retry-After": "1"})
        if req.path.endswith("/artist/"):
            name = req.args["query"].split('"')[1]
            if name == "Pixies":
                return Response(json.dumps({"artists": [{"id": "mb-pixies", "score": 100}]}), 200)
            return Response(json.dumps({"artists": [{"id": "mb-wrong", "score": 40}]}), 200)
        if req.path.endswith("/artist/mb-pixies"):
            assert "genres" in req.args["inc"]
            return Response(json.dumps({"genres": [{"name": "Alternative Rock"}, {"name": "Noise Pop"}]}), 200)
        return Response("{}", 404)


def test_mb_genres_via_lookup_with_retry_and_no_failure_caching(conn, plex, httpserver, monkeypatch):
    sync_all(conn, plex, full=True)
    httpserver.clear()  # drop the fake-Plex catch-all; this server now plays MusicBrainz
    mb = FakeMB()
    httpserver.expect_request("/ws/2/artist/", method="GET").respond_with_handler(mb)
    httpserver.expect_request("/ws/2/artist/mb-pixies", method="GET").respond_with_handler(mb)
    monkeypatch.setattr(enrich_mod, "MB_ROOT", httpserver.url_for("/ws/2/"))
    monkeypatch.setattr(enrich_mod.time, "sleep", lambda s: None)

    mb.fail_next = 1  # first request is rate-limited, then retried
    stats = enrich(conn, CFG)
    assert stats["artists_processed"] == 2

    tags = {r[0] for r in conn.execute(
        "SELECT DISTINCT tag FROM track_tags tt JOIN tracks t ON t.id = tt.track_id "
        "WHERE tt.source = 'musicbrainz' AND t.artist_name = 'Pixies'")}
    assert tags == {"alternative rock", "noise pop"}
    # low-score match for Slowdive is ignored rather than mis-tagging it
    assert conn.execute("SELECT mbid FROM artists WHERE name = 'Slowdive'").fetchone()[0] is None
    assert conn.execute("SELECT mbid FROM artists WHERE name = 'Pixies'").fetchone()[0] == "mb-pixies"
    assert conn.execute("SELECT COUNT(*) FROM enrich_cache WHERE payload IS NULL").fetchone()[0] == 0

    # second run: everything cached, known MBID skips the search
    mb.calls.clear()
    enrich(conn, CFG)
    assert mb.calls == []


def test_persistent_failure_is_not_cached(conn, plex, httpserver, monkeypatch):
    sync_all(conn, plex, full=True)
    httpserver.clear()
    httpserver.expect_request("/ws/2/artist/").respond_with_response(Response("down", 503))
    monkeypatch.setattr(enrich_mod, "MB_ROOT", httpserver.url_for("/ws/2/"))
    monkeypatch.setattr(enrich_mod.time, "sleep", lambda s: None)
    enrich(conn, CFG)
    assert conn.execute("SELECT COUNT(*) FROM enrich_cache").fetchone()[0] == 0
