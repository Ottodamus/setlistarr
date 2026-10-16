import json

import pytest
from fastapi.testclient import TestClient

from setlistarr.core.plex.sync import sync_all
from setlistarr.generation.constraints import Constraints
from setlistarr.llm.registry import ProviderConfig, save_provider
from setlistarr.services import recipes as svc
from setlistarr.web.app import create_app

from .fakes.scripted_provider import ScriptedProvider, call, say, tools


@pytest.fixture
def lib(conn, plex, configured):
    sync_all(conn, plex, full=True)
    save_provider(conn, ProviderConfig(id=None, name="Claude", kind="anthropic", model="claude-opus-5-5"))
    return conn


def make_recipe(conn, **over):
    data = {"name": "Shoegaze weekly", "prompt": "dreamy shoegaze", "provider_id": None, "mode": None,
            "constraints": Constraints.from_form({"target_minutes": 7}), "schedule_cron": "0 6 * * 1",
            "enabled": True, "avoid_repeats": True, "playlist_title": None, **over}
    return svc.save_recipe(conn, data)


def agent(*keys):
    return ScriptedProvider([tools(call("add_to_draft", plex_keys=list(keys))),
                             tools(call("finish", title="t", reasoning="r"))])


def test_first_run_creates_then_refresh_replaces_in_place(lib, fake_plex):
    rid = make_recipe(lib)
    first = svc.run_recipe(rid, inject={"provider": agent("100", "101")})
    assert first["status"] == "ok" and first["action"] == "created", first
    key = svc.get_recipe(lib, rid)["plex_playlist_key"]
    assert fake_plex.playlists[key]["title"] == "Shoegaze weekly"

    second_agent = agent("100", "102", "103")
    second = svc.run_recipe(rid, inject={"provider": second_agent})
    assert second["action"] == "replaced"
    r = svc.get_recipe(lib, rid)
    assert r["plex_playlist_key"] == key and r["last_status"] == "ok"
    # avoid_repeats: last refresh's tracks were rejected, and the agent was told about them
    assert fake_plex.playlists[key]["_keys"] == ["102", "103"]
    assert "previous refresh" in second_agent.requests[0]["messages"][0]["content"]
    assert lib.execute("SELECT recipe_id FROM managed_playlists WHERE plex_playlist_key = ?", (key,)).fetchone()[0] == rid
    job = lib.execute("SELECT kind, status FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(job) == ("recipe", "ok")


def test_provider_error_marks_recipe_failed(lib, fake_plex):
    from setlistarr.llm.base import ProviderError

    class Boom:
        kind, model = "x", "claude-opus-5-5"

        def chat(self, **kw):
            raise ProviderError("Anthropic rejected the API key")

    rid = make_recipe(lib)
    res = svc.run_recipe(rid, inject={"provider": Boom()})
    assert res["status"] == "error" and "API key" in res["error"]
    assert fake_plex.playlists == {}  # nothing created
    assert svc.get_recipe(lib, rid)["last_status"] == "error"


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_recipe_pages_and_scheduling(client, lib):
    r = client.post("/recipes/save", data={"name": "Mon mix", "prompt": "energetic", "schedule_cron": "0 6 * * 1",
                                           "enabled": "on", "avoid_repeats": "on", "target_minutes": "45"},
                    follow_redirects=False)
    assert r.status_code == 303
    rid = lib.execute("SELECT id FROM recipes WHERE name='Mon mix'").fetchone()[0]
    assert client.app.state.scheduler.recipe_next_runs().keys() == {rid}
    page = client.get("/recipes").text
    assert "Mon mix" in page and "Weekly, Monday 6:00" in page
    c = json.loads(lib.execute("SELECT constraints_json FROM recipes").fetchone()[0])
    assert c["target_minutes"] == 45 and c["locked"] == ["target_minutes"]

    r = client.post("/recipes/save", data={"name": "Bad", "prompt": "x", "schedule_cron_custom": "not cron"})
    assert r.status_code == 400 and "valid cron" in r.text

    client.post(f"/recipes/{rid}/toggle")
    assert client.app.state.scheduler.recipe_next_runs() == {}
    assert "/recipes/new?draft_id=" not in page  # sanity: link lives in the draft editor
    assert client.get("/").status_code == 200 and "Upcoming refreshes" in client.get("/").text
