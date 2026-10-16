import json

import pytest
from fastapi.testclient import TestClient

from setlistarr.core.plex.client import PlexClient, PlexError
from setlistarr.core.plex.sync import sync_all
from setlistarr.generation.constraints import Constraints
from setlistarr.generation.runner import GenerationManager
from setlistarr.generation.validate import finalize
from setlistarr.llm.registry import ProviderConfig, save_provider
from setlistarr.services import drafts as svc
from setlistarr.services.generations import get_generation, load_draft
from setlistarr.services.push import push_draft
from setlistarr.web.app import create_app

from .fakes.fake_plex import TOKEN
from .fakes.scripted_provider import ScriptedProvider, call, tools


@pytest.fixture
def lib(conn, plex, configured):
    sync_all(conn, plex, full=True)
    save_provider(conn, ProviderConfig(id=None, name="Claude", kind="anthropic", model="claude-opus-5-5"))
    return conn


def make_draft(conn, keys, constraints=None, locked=()):
    """A finished generation + draft without running a model."""
    script = ScriptedProvider([
        tools(call("add_to_draft", plex_keys=keys)),
        tools(call("finish", title="Test Mix", reasoning="r")),
    ])
    gen_id = GenerationManager().start(conn, "test prompt", background=False, inject={"provider": script},
                                       constraints=constraints or Constraints(target_minutes=10, max_per_artist=3))
    draft_id = get_generation(conn, gen_id)["draft_id"]
    for k in locked:
        svc.toggle_lock(conn, draft_id, k)
    return draft_id


def client_for(fake_plex):
    return PlexClient(fake_plex.url, TOKEN)


# --------------------------------------------------------------------- push
def test_push_creates_then_replaces_in_place(lib, fake_plex):
    draft_id = make_draft(lib, ["100", "102", "104"])
    first = push_draft(lib, draft_id, title="Road Trip", summary="vibes", plex=client_for(fake_plex))
    assert first["action"] == "created"
    key = first["ratingKey"]
    assert fake_plex.playlists[key]["_keys"] == ["100", "102", "104"]

    svc.reorder(lib, draft_id, ["104", "100", "102"])
    svc.remove_track(lib, draft_id, "100")
    second = push_draft(lib, draft_id, title="Road Trip v2", replace_key=key, plex=client_for(fake_plex))
    assert second["action"] == "replaced" and second["ratingKey"] == key  # same ratingKey: pins survive
    pl = fake_plex.playlists[key]
    assert pl["_keys"] == ["104", "102"] and pl["title"] == "Road Trip v2"
    assert len(fake_plex.playlists) == 1
    assert fake_plex.requests_matching(rf"DELETE /playlists/{key}/items$|DELETE /playlists/{key}/items\?")


def test_replace_falls_back_to_per_item_delete(lib, fake_plex):
    fake_plex.bulk_clear = False
    draft_id = make_draft(lib, ["100", "102", "104"])
    key = push_draft(lib, draft_id, title="X", plex=client_for(fake_plex))["ratingKey"]
    svc.remove_track(lib, draft_id, "104")
    push_draft(lib, draft_id, title="X", replace_key=key, plex=client_for(fake_plex))
    assert fake_plex.playlists[key]["_keys"] == ["100", "102"]
    assert len(fake_plex.requests_matching(rf"DELETE /playlists/{key}/items/\d+")) == 3


def test_never_touches_unmanaged_playlists(lib, fake_plex):
    fake_plex.playlists["777"] = {"ratingKey": "777", "title": "My hand-made list", "_keys": ["105"], "_ids": [1]}
    draft_id = make_draft(lib, ["100", "102"])
    with pytest.raises(PlexError, match="only replaces playlists it created"):
        push_draft(lib, draft_id, title="My hand-made list", replace_key="777", plex=client_for(fake_plex))
    assert fake_plex.playlists["777"]["_keys"] == ["105"]
    # and pushing a *new* playlist with the same title doesn't touch it either
    push_draft(lib, draft_id, title="My hand-made list", plex=client_for(fake_plex))
    assert fake_plex.playlists["777"]["_keys"] == ["105"] and len(fake_plex.playlists) == 2


def test_replace_recreates_when_deleted_in_plex(lib, fake_plex):
    draft_id = make_draft(lib, ["100", "102"])
    key = push_draft(lib, draft_id, title="X", plex=client_for(fake_plex))["ratingKey"]
    del fake_plex.playlists[key]  # user deleted it in Plexamp
    res = push_draft(lib, draft_id, title="X", replace_key=key, plex=client_for(fake_plex))
    assert res["action"] == "recreated" and res["ratingKey"] != key
    managed = [r[0] for r in lib.execute("SELECT plex_playlist_key FROM managed_playlists")]
    assert managed == [res["ratingKey"]]


def test_push_skips_tracks_removed_from_plex(lib, fake_plex):
    draft_id = make_draft(lib, ["100", "102"])
    lib.execute("UPDATE tracks SET removed_at = '2026-01-01' WHERE plex_key = '102'")
    lib.commit()
    res = push_draft(lib, draft_id, title="X", plex=client_for(fake_plex))
    assert res["skipped"] == ["102"] and fake_plex.playlists[res["ratingKey"]]["_keys"] == ["100"]


# ------------------------------------------------------------------ editing
def test_editing_ops_and_warnings(lib):
    draft_id = make_draft(lib, ["100", "102"])
    svc.add_track(lib, draft_id, "103", position=1)
    with pytest.raises(svc.DraftError, match="already in the draft"):
        svc.add_track(lib, draft_id, "103")
    with pytest.raises(svc.DraftError, match="isn't in the library"):
        svc.add_track(lib, draft_id, "bogus")
    with pytest.raises(svc.DraftError, match="changed in another tab"):
        svc.reorder(lib, draft_id, ["100"])
    d = svc.load_for_editor(lib, draft_id)
    assert [t["plex_key"] for t in d["tracks"]] == ["100", "103", "102"]
    assert [t["locked"] for t in d["tracks"]] == [False, True, False]  # hand-added starts locked

    svc.add_track(lib, draft_id, "104")
    svc.add_track(lib, draft_id, "105")  # 4 Pixies tracks > cap 3: allowed, but warned
    w = svc.load_for_editor(lib, draft_id)["warnings"]
    assert any("More than 3 tracks by: Pixies" in x for x in w)


def test_finalize_never_drops_or_trims_locked(lib):
    c = Constraints(target_minutes=7, max_per_artist=1)  # tiny window, cap 1
    out = finalize(lib, ["104", "105", "100"], c, pool=[], keep={"102", "103"})
    keys = [t["plex_key"] for t in out.tracks]
    assert {"102", "103"} <= set(keys)  # two Pixies tracks kept despite cap 1
    assert "104" not in keys and "105" not in keys  # model's Pixies picks hit the cap


def test_regenerate_keeps_locked_tracks_and_agent_cannot_remove_them(lib):
    draft_id = make_draft(lib, ["100", "102"], locked=["102"])
    script = ScriptedProvider([
        tools(call("remove_from_draft", plex_keys=["102"])),
        tools(call("add_to_draft", plex_keys=["101"])),
        tools(call("finish", title="Again", reasoning="r")),
    ])
    gen_id = GenerationManager().start(lib, "again", background=False, inject={"provider": script},
                                       seed_keys=["102"], parent_draft_id=draft_id,
                                       constraints=Constraints(target_minutes=7))
    g = get_generation(lib, gen_id)
    assert g["status"] == "ok" and g["parent_draft_id"] == draft_id
    remove_result = json.loads(script.requests[1]["messages"][-1]["content"])
    assert remove_result["removed"] == 0 and remove_result["not_removed"]["plex_keys"] == ["102"]
    assert "102: Pixies - Debaser" in script.requests[0]["messages"][0]["content"]
    new = svc.load_for_editor(lib, g["draft_id"])
    assert [(t["plex_key"], t["locked"]) for t in new["tracks"]][:2] == [("102", True), ("101", False)]


# ---------------------------------------------------------------------- web
@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_editor_and_push_over_http(client, lib, fake_plex, monkeypatch):
    draft_id = make_draft(lib, ["100", "102", "104"])
    html = client.get(f"/drafts/{draft_id}/editor").text
    assert 'class="sortable-body"' in html and "Create a new playlist" in html

    html = client.post(f"/drafts/{draft_id}/reorder", data={"key": ["104", "100", "102"]}).text
    assert html.index("Planet of Sound") < html.index("Alison")
    client.post(f"/drafts/{draft_id}/remove", data={"plex_key": "100"})
    assert "Hey" in client.get(f"/drafts/{draft_id}/search?q=hey").text
    html = client.post(f"/drafts/{draft_id}/add", data={"plex_key": "103"}).text
    assert "Hey" in html and "🔒" in html
    html = client.post(f"/drafts/{draft_id}/meta", data={"title": "", "description": ""}).text
    assert "Title can&#39;t be empty" in html or "Title can't be empty" in html

    html = client.post(f"/drafts/{draft_id}/push", data={"title": "HTTP Mix", "target": "new"}).text
    assert "Created <b>HTTP Mix</b>" in html and "Open in Plex" in html
    key = next(iter(fake_plex.playlists))
    assert f'value="{key}" selected' in html  # next push defaults to replacing it

    hist = client.get("/history").text
    assert "HTTP Mix" in hist and "pushed" in hist and "AI spend" in hist
    assert client.get(f"/drafts/{draft_id}", follow_redirects=False).headers["location"].endswith("#draft")


def test_regenerate_endpoint_redirects_to_new_generation(client, lib, monkeypatch):
    draft_id = make_draft(lib, ["100", "102"], locked=["100"])
    from setlistarr.generation import runner

    started = {}
    monkeypatch.setattr(runner.manager, "start", lambda conn, prompt, **kw: started.update(prompt=prompt, **kw) or 99)
    r = client.post(f"/drafts/{draft_id}/regenerate", data={"prompt": "more shoegaze"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/generate/99"
    assert started["prompt"] == "more shoegaze" and started["seed_keys"] == ["100"]
    assert started["parent_draft_id"] == draft_id
