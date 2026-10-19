import re
import time

import pytest
from fastapi.testclient import TestClient

from setlistarr.core.plex.sync import sync_all
from setlistarr.generation.runner import manager
from setlistarr.llm.registry import list_providers
from setlistarr.web.app import create_app

from .fakes.scripted_provider import ScriptedProvider, call, tools


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def _add_provider(client, **over):
    data = {"kind": "anthropic", "name": "Claude", "model": "claude-opus-5-5", "base_url": "",
            "api_key": "sk-secret", "tool_mode": "auto", "opt_effort": "high", "fallbacks": "on", **over}
    return client.post("/settings/providers/save", data=data, follow_redirects=False)


def test_provider_crud_and_secret_handling(client, conn):
    assert _add_provider(client).status_code == 303
    [p] = list_providers(conn)
    assert p.is_default and p.api_key == "sk-secret" and p.options == {"effort": "high", "fallbacks": True}

    page = client.get("/settings/providers").text
    assert "sk-secret" not in page and "claude-opus-5-5" in page
    assert "sk-secret" not in client.get(f"/settings/providers/{p.id}").text

    # blank key on edit keeps it; options re-parsed
    _add_provider(client, id=str(p.id), api_key="", opt_effort="", model="claude-sonnet-5-5")
    [p] = list_providers(conn)
    assert p.api_key == "sk-secret" and p.model == "claude-sonnet-5-5" and "effort" not in p.options

    # kind switch re-renders the form with that kind's defaults
    form = client.get("/settings/providers/form?kind=ollama").text
    assert "num_ctx" in form and "localhost:11434" in form and "api_key" not in form

    client.post(f"/settings/providers/{p.id}/delete")
    assert list_providers(conn) == []


def test_pricing_edit(client, conn):
    client.post("/settings/pricing", data={"model": "gpt-test", "input_per_mtok": "1.5", "output_per_mtok": "6"})
    row = conn.execute("SELECT * FROM model_pricing WHERE model='gpt-test'").fetchone()
    assert row["input_per_mtok"] == 1.5 and row["cache_read_per_mtok"] is None and row["note"] == "user"


def test_generate_without_provider_shows_error(client):
    r = client.post("/generate", data={"prompt": "anything"})
    assert r.status_code == 400 and "No AI provider" in r.text


def test_generate_flow_end_to_end(client, conn, plex, monkeypatch):
    sync_all(conn, plex, full=True)
    _add_provider(client)
    script = ScriptedProvider([
        tools(call("search_tracks", any_tags=["shoegaze"])),
        tools(call("add_to_draft", plex_keys=["100", "101"])),
        tools(call("finish", title="Haze", reasoning="Dreamy.")),
    ])
    real_start = manager.start
    monkeypatch.setattr(manager, "start", lambda *a, **kw: real_start(*a, inject={"provider": script}, **kw))

    r = client.post("/generate", data={"prompt": "dreamy", "target_minutes": "7", "max_per_artist": "2"},
                    follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    gen_id = int(url.rsplit("/", 1)[1])
    for _ in range(100):
        if manager.events(gen_id) and manager.events(gen_id).done:
            break
        time.sleep(0.05)

    stream = client.get(f"{url}/events").text
    assert "event: step" in stream and "added 2 track(s)" in stream and stream.rstrip().endswith("data:")
    assert re.search(r"event: done", stream)

    page = client.get(url).text
    assert "Haze" in page and "/editor" in page  # editor is lazy-loaded into the page
    draft_id = re.search(r"/drafts/(\d+)/editor", page).group(1)
    editor = client.get(f"/drafts/{draft_id}/editor").text
    assert "Alison" in editor and "Machine Gun" in editor and "Regenerate" in editor
    assert "Haze" in client.get("/generate").text  # recent list


def test_cost_hint(client, conn):
    from setlistarr.llm.registry import cost_hint, get_provider

    _add_provider(client)
    p = get_provider(conn)
    assert cost_hint(conn, p).startswith("$4 in / $20 out")
    conn.execute("INSERT INTO generations(created_at, prompt, provider_id, model, status, cost_usd) "
                 "VALUES('2026-01-01', 'x', ?, 'claude-opus-5-5', 'ok', 0.12)", (p.id,))
    conn.commit()
    assert cost_hint(conn, p).startswith("~$0.120 per run (avg of last 1)")
    assert "~$0.120" in client.get("/generate").text


def test_recent_shows_current_playlist_title(client, conn, plex, monkeypatch):
    from setlistarr.core.plex.sync import sync_all
    from setlistarr.generation.constraints import Constraints
    from setlistarr.generation.runner import GenerationManager
    from setlistarr.services import drafts as dsvc
    from setlistarr.services.generations import get_generation

    sync_all(conn, plex, full=True)
    _add_provider(client)
    script = ScriptedProvider([tools(call("add_to_draft", plex_keys=["100", "101"])),
                               tools(call("finish", title="Model Title", reasoning="r"))])
    gid = GenerationManager().start(conn, "a prompt nobody reads", background=False, inject={"provider": script},
                                    constraints=Constraints(target_minutes=7))
    dsvc.update_meta(conn, get_generation(conn, gid)["draft_id"], "My Renamed Mix", "")
    page = client.get("/generate").text
    recent = page[page.index("<h2>Recent</h2>"):]
    assert "My Renamed Mix" in recent and "2 tracks" in recent and "Model Title" not in recent
