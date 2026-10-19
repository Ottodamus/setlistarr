import pytest
from fastapi.testclient import TestClient

from setlistarr.core.plex.client import PlexClient
from setlistarr.core.plex.playlist_sync import sync_playlists
from setlistarr.core.plex.sync import sync_all
from setlistarr.generation.constraints import Constraints
from setlistarr.generation.runner import GenerationManager
from setlistarr.llm.registry import ProviderConfig, save_provider
from setlistarr.services.generations import get_generation
from setlistarr.services.library import browse_artists, browse_tracks, playlist_choices
from setlistarr.services.push import push_draft
from setlistarr.web.app import create_app

from .fakes.fake_plex import TOKEN
from .fakes.scripted_provider import ScriptedProvider, call, tools


@pytest.fixture
def lib(conn, plex, configured, fake_plex):
    sync_all(conn, plex, full=True)
    fake_plex.add_user_playlist("501", "Gym", ["102", "103"])
    fake_plex.add_user_playlist("502", "Chill Sundays", ["100", "102"])
    return conn


def keys(rows):
    return sorted(r["plex_key"] for r in rows)


def test_sync_reads_all_playlists_and_skips_unchanged(lib, plex, fake_plex):
    assert sync_playlists(lib, plex) == {"playlists": 2, "reread": 2, "removed": 0}
    fake_plex.log.clear()
    assert sync_playlists(lib, plex)["reread"] == 0
    assert not fake_plex.requests_matching(r"/items")  # unchanged playlists aren't re-read

    del fake_plex.playlists["502"]
    fake_plex.playlists["501"]["_keys"].append("104")
    fake_plex.playlists["501"]["_ids"].append(fake_plex._item_id())
    fake_plex.playlists["501"]["updatedAt"] = 200
    assert sync_playlists(lib, plex) == {"playlists": 1, "reread": 1, "removed": 1}
    assert keys(browse_tracks(lib, playlist="501")["rows"]) == ["102", "103", "104"]
    assert browse_tracks(lib, playlist="502")["total"] == 0


def test_filters_chips_and_artists(lib, plex):
    sync_playlists(lib, plex)
    assert keys(browse_tracks(lib, playlist="in")["rows"]) == ["100", "102", "103"]
    assert keys(browse_tracks(lib, playlist="none")["rows"]) == ["101", "104", "105"]
    debaser = next(r for r in browse_tracks(lib, q="debaser")["rows"])
    assert [p["title"] for p in debaser["playlists"]] == ["Chill Sundays", "Gym"]
    assert not any(p["managed"] for p in debaser["playlists"])
    pixies = next(a for a in browse_artists(lib)["rows"] if a["name"] == "Pixies")
    assert [p["title"] for p in pixies["playlists"]] == ["Chill Sundays", "Gym"]
    slowdive = next(a for a in browse_artists(lib)["rows"] if a["name"] == "Slowdive")
    assert [p["title"] for p in slowdive["playlists"]] == ["Chill Sundays"]
    assert [(c["title"], c["n"]) for c in playlist_choices(lib)] == [("Chill Sundays", 2), ("Gym", 2)]


def test_push_updates_membership_immediately_and_marks_managed(lib, plex, fake_plex):
    save_provider(lib, ProviderConfig(id=None, name="Claude", kind="anthropic", model="claude-opus-5-5"))
    script = ScriptedProvider([tools(call("add_to_draft", plex_keys=["101", "104"])),
                               tools(call("finish", title="Fresh", reasoning="r"))])
    gid = GenerationManager().start(lib, "x", background=False, inject={"provider": script},
                                    constraints=Constraints(target_minutes=7))
    res = push_draft(lib, get_generation(lib, gid)["draft_id"], title="Fresh", plex=PlexClient(fake_plex.url, TOKEN))
    row = next(r for r in browse_tracks(lib, playlist=res["ratingKey"])["rows"] if r["plex_key"] == "101")
    assert row["playlists"] == [{"key": res["ratingKey"], "title": "Fresh", "managed": True}]


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_library_page_shows_filter_and_chips(client, lib, plex):
    sync_playlists(lib, plex)
    page = client.get("/library").text
    assert 'name="playlist"' in page and "Gym (2)" in page and "Not in any playlist" in page
    assert 'href="/library?playlist=501"' in page and "pl-dot" in page
    only_gym = client.get("/library?playlist=501", headers={"HX-Request": "true"}).text
    assert "Debaser" in only_gym and "Hey" in only_gym and "Alison" not in only_gym
    assert "Chill Sundays" in client.get("/library/artists").text
