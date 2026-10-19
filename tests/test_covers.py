import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from setlistarr.core.plex.client import PlexClient
from setlistarr.core.plex.sync import sync_all
from setlistarr.generation.constraints import Constraints
from setlistarr.generation.runner import GenerationManager
from setlistarr.llm.base import ChatResponse, ProviderError, Usage
from setlistarr.llm.registry import ProviderConfig, get_provider, save_provider, text_providers
from setlistarr.services import covers as svc
from setlistarr.services import recipes as recipes_svc
from setlistarr.services.generations import get_generation
from setlistarr.services.push import push_draft
from setlistarr.web.app import create_app

from .fakes.fake_plex import TOKEN
from .fakes.scripted_provider import ScriptedProvider, call, tools


def png(w=600, h=400, color=(255, 138, 61, 255), mode="RGBA"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, "PNG")
    return buf.getvalue()


GOOD_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">'
            '<defs><linearGradient id="g"><stop offset="0" stop-color="#1a1029"/><stop offset="1" stop-color="#ff5cad"/>'
            '</linearGradient></defs><rect width="1024" height="1024" fill="url(#g)"/><circle cx="512" cy="512" r="300" fill="#ff8a3d"/></svg>')


class FakeImageModel:
    kind, model = "openai_compat", "gpt-image-2.5-flare"

    def __init__(self):
        self.prompts = []

    def generate_image(self, prompt, size="1024x1024"):
        self.prompts.append(prompt)
        return png(1024, 1024, (40, 90, 200, 255)), Usage(input_tokens=100, output_tokens=4000), self.model


@pytest.fixture
def lib(conn, plex, configured):
    sync_all(conn, plex, full=True)
    save_provider(conn, ProviderConfig(id=None, name="Claude", kind="anthropic", model="claude-opus-5-5"))
    save_provider(conn, ProviderConfig(id=None, name="Images", kind="openai_compat", model="gpt-image-2.5-flare",
                                       base_url="https://api.openai.com/v1"))
    return conn


def make_draft(conn):
    script = ScriptedProvider([tools(call("add_to_draft", plex_keys=["100", "102"])),
                               tools(call("finish", title="Night Drive", reasoning="r"))])
    gid = GenerationManager().start(conn, "night drive", background=False, inject={"provider": script},
                                    constraints=Constraints(target_minutes=7))
    return get_generation(conn, gid)["draft_id"]


def test_normalize_squares_flattens_and_validates():
    out = Image.open(io.BytesIO(svc.normalize(png(1600, 900))))
    assert out.format == "JPEG" and out.size == (900, 900) and out.mode == "RGB"
    big = Image.open(io.BytesIO(svc.normalize(png(3000, 3000))))
    assert big.size == (1024, 1024)
    with pytest.raises(svc.CoverError, match="too small"):
        svc.normalize(png(100, 100))
    with pytest.raises(svc.CoverError, match="isn't an image"):
        svc.normalize(b"not an image at all")


def test_image_models_are_cover_only(lib):
    assert [p.name for p in text_providers(lib)] == ["Claude"]
    assert get_provider(lib).name == "Claude"  # never defaults to an image model for playlists
    flare = next(p for p in svc.cover_providers(lib) if p["method"] == "image")
    assert flare["cfg"].is_image_model and flare["cfg"].deprecation is None
    old = ProviderConfig(id=None, name="old", kind="openai_compat", model="chatgpt-image-latest")
    assert old.deprecation[0] == "2026-12-01"


def test_generate_with_image_model_costs_and_stores(lib):
    did = make_draft(lib)
    fake = FakeImageModel()
    cid = svc.generate(lib, prompt="neon highway at night", provider_id=2, draft_id=did, inject={"provider": fake})
    c = svc.get_cover(lib, cid)
    assert c["kind"] == "ai_image" and c["model"] == "gpt-image-2.5-flare"
    assert c["cost_usd"] == pytest.approx((100 * 5 + 4000 * 30) / 1e6)  # seeded token prices
    assert (svc.covers_dir() / c["file"]).stat().st_size > 0


def test_generate_designed_art_retries_bad_svg(lib):
    did = make_draft(lib)
    llm = ScriptedProvider([ChatResponse(text="Here's a nice cover idea!", usage=Usage(500, 200)),
                            ChatResponse(text=f"```svg\n{GOOD_SVG}\n```", usage=Usage(600, 900))])
    cid = svc.generate(lib, prompt="sunset synthwave", provider_id=1, draft_id=did, inject={"provider": llm})
    c = svc.get_cover(lib, cid)
    assert c["kind"] == "ai_art" and c["cost_usd"] > 0 and len(llm.requests) == 2
    img = Image.open(svc.covers_dir() / c["file"])
    assert img.size == (1024, 1024) and img.getpixel((512, 512))[0] > 200  # the orange circle rendered


def test_push_uploads_cover_only_when_it_changes(lib, fake_plex):
    did = make_draft(lib)
    plex = lambda: PlexClient(fake_plex.url, TOKEN)  # noqa: E731
    first = push_draft(lib, did, title="Night Drive", plex=plex())
    assert first["cover"] == "none" and fake_plex.posters == {}  # no cover -> Plex's own art untouched

    cid = svc.store(lib, png(), kind="upload", draft_id=did)
    svc.set_cover(lib, cid, draft_id=did)
    key = first["ratingKey"]
    assert push_draft(lib, did, title="Night Drive", replace_key=key, plex=plex())["cover"] == "uploaded"
    assert push_draft(lib, did, title="Night Drive", replace_key=key, plex=plex())["cover"] == "unchanged"
    assert len(fake_plex.posters[key]) == 1 and fake_plex.posters[key][0][:2] == b"\xff\xd8"  # one JPEG upload


def test_recipe_renew_makes_new_cover_each_run_and_survives_failure(lib, fake_plex):
    rid = recipes_svc.save_recipe(lib, {
        "name": "Weekly", "prompt": "dreamy", "provider_id": 1, "mode": None,
        "constraints": Constraints.from_form({"target_minutes": 7}), "schedule_cron": "", "enabled": True,
        "avoid_repeats": False, "cover_mode": "renew", "cover_prompt": None, "cover_provider_id": 2})

    def agent():
        return ScriptedProvider([tools(call("add_to_draft", plex_keys=["100", "101"])),
                                 tools(call("finish", title="Dream Weekly", reasoning="r"))])

    img = FakeImageModel()
    r1 = recipes_svc.run_recipe(rid, inject={"provider": agent(), "cover_inject": {"provider": img}})
    r2 = recipes_svc.run_recipe(rid, inject={"provider": agent(), "cover_inject": {"provider": img}})
    assert r1["status"] == r2["status"] == "ok" and r1["cover"] == r2["cover"] == "uploaded"
    key = recipes_svc.get_recipe(lib, rid)["plex_playlist_key"]
    assert len(fake_plex.posters[key]) == 2 and len(img.prompts) == 2
    assert "Dream Weekly" in img.prompts[0]  # auto prompt built from the new playlist

    class Broken:
        def generate_image(self, prompt, size="1024x1024"):
            raise ProviderError("OpenAI refused this cover prompt (content policy)")

    before = recipes_svc.get_recipe(lib, rid)["cover_id"]
    r3 = recipes_svc.run_recipe(rid, inject={"provider": agent(), "cover_inject": {"provider": Broken()}})
    r = recipes_svc.get_recipe(lib, rid)
    assert r3["status"] == "ok" and "kept the previous one" in r3["cover_error"]
    assert r["cover_id"] == before and r["last_status"] == "ok" and "content policy" in r["last_error"]


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_widget_upload_select_download(client, lib):
    did = make_draft(lib)
    html = client.get(f"/covers/widget?draft_id={did}").text
    assert "Upload an image" in html and "What to expect" in html and "Artwork (image model)" in html
    assert 'music playlist titled &#34;Night Drive&#34;' in html  # auto prompt, HTML-escaped

    r = client.post("/covers/upload", data={"draft_id": str(did)}, files={"file": ("x.png", png(800, 800), "image/png")})
    assert "Cover uploaded." in r.text
    cid = lib.execute("SELECT cover_id FROM drafts WHERE id = ?", (did,)).fetchone()[0]
    img = client.get(f"/covers/{cid}.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    dl = client.get(f"/covers/{cid}.jpg?download=1")
    assert f'night-drive-{cid}.jpg' in dl.headers["content-disposition"]

    bad = client.post("/covers/upload", data={"draft_id": str(did)}, files={"file": ("x.txt", b"hello", "text/plain")})
    assert "isn&#39;t an image" in bad.text or "isn't an image" in bad.text
    client.post("/covers/select", data={"draft_id": str(did), "cover_id": "none"})
    assert lib.execute("SELECT cover_id FROM drafts WHERE id = ?", (did,)).fetchone()[0] is None
    assert client.get("/covers/99999.jpg").status_code == 404


def test_generate_can_make_a_cover_in_the_same_run(lib):
    img = FakeImageModel()
    script = ScriptedProvider([tools(call("add_to_draft", plex_keys=["100", "101"])),
                               tools(call("finish", title="Shoegaze Dusk", reasoning="hazy guitars"))])
    gid = GenerationManager().start(lib, "dreamy shoegaze", background=False, constraints=Constraints(target_minutes=7),
                                    cover={"prompt": None, "provider_id": 2},
                                    inject={"provider": script, "cover_inject": {"provider": img}})
    g = get_generation(lib, gid)
    assert g["status"] == "ok"
    cover_id = lib.execute("SELECT cover_id FROM drafts WHERE id = ?", (g["draft_id"],)).fetchone()[0]
    assert svc.get_cover(lib, cover_id)["kind"] == "ai_image"
    assert 'titled "Shoegaze Dusk"' in img.prompts[0] and "hazy guitars" in img.prompts[0]
    assert [e["msg"] for e in g["trace"] if e["kind"] == "draft"][-1] == "cover ready"


def test_cover_failure_never_fails_the_playlist(lib):
    class Broken:
        def generate_image(self, prompt, size="1024x1024"):
            raise ProviderError("OpenAI rate limit hit - try again shortly")

    script = ScriptedProvider([tools(call("add_to_draft", plex_keys=["100", "101"])),
                               tools(call("finish", title="t", reasoning="r"))])
    gid = GenerationManager().start(lib, "x", background=False, constraints=Constraints(target_minutes=7),
                                    cover={"prompt": "neon city", "provider_id": 2},
                                    inject={"provider": script, "cover_inject": {"provider": Broken()}})
    g = get_generation(lib, gid)
    d = lib.execute("SELECT cover_id, adjustments_json FROM drafts WHERE id = ?", (g["draft_id"],)).fetchone()
    assert g["status"] == "ok" and d["cover_id"] is None
    assert "Cover wasn't generated: OpenAI rate limit hit" in d["adjustments_json"]


def test_generate_form_passes_cover_options(client, lib, monkeypatch):
    from setlistarr.generation import runner

    started = {}
    monkeypatch.setattr(runner.manager, "start", lambda conn, prompt, **kw: started.update(kw) or 1)
    page = client.get("/generate").text
    assert "Cover (optional)" in page and "Also generate a cover" in page and "(artwork)" in page
    client.post("/generate", data={"prompt": "x", "cover_enabled": "on", "cover_prompt": " vinyl sunset ",
                                   "cover_provider_id": "2"}, follow_redirects=False)
    assert started["cover"] == {"prompt": "vinyl sunset", "provider_id": 2}
    client.post("/generate", data={"prompt": "x"}, follow_redirects=False)
    assert started["cover"] is None
