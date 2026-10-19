"""Cover widget endpoints. The widget belongs to either a draft or a recipe (owner)."""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse

from ...llm.base import ProviderError
from ...services import covers as svc
from ..deps import get_conn, templates

router = APIRouter(prefix="/covers")


def _owner(data) -> tuple[int | None, int | None]:
    d = str(data.get("draft_id") or "")
    r = str(data.get("recipe_id") or "")
    draft_id, recipe_id = (int(d) if d.isdigit() else None), (int(r) if r.isdigit() else None)
    if not draft_id and not recipe_id:
        raise HTTPException(400, "draft_id or recipe_id is required")
    return draft_id, recipe_id


def _context(conn, draft_id, recipe_id) -> dict:
    if draft_id:
        row = conn.execute(
            "SELECT d.cover_id, d.title, d.description, g.prompt FROM drafts d "
            "LEFT JOIN generations g ON g.id = d.generation_id WHERE d.id = ?", (draft_id,)).fetchone()
    else:
        row = conn.execute("SELECT cover_id, name AS title, NULL AS description, prompt, cover_prompt, "
                           "cover_provider_id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    row = dict(row)
    providers = svc.cover_providers(conn)
    default = svc.default_cover_provider(conn)
    return {
        "draft_id": draft_id, "recipe_id": recipe_id,
        "owner_q": f"draft_id={draft_id}" if draft_id else f"recipe_id={recipe_id}",
        "cover": svc.get_cover(conn, row["cover_id"]),
        "history": svc.history(conn, draft_id=draft_id, recipe_id=recipe_id),
        "prompt": row.get("cover_prompt") or svc.default_prompt(row["title"], row["description"], row["prompt"]),
        "providers": providers,
        "selected_provider": row.get("cover_provider_id") or (default.id if default else None),
        "deprecated": [p["cfg"] for p in providers if p["cfg"].deprecation],
        "title": row["title"],
    }


def render_widget(request: Request, conn, draft_id, recipe_id, **extra):
    return templates.TemplateResponse(request, "partials/cover_widget.html",
                                      {**_context(conn, draft_id, recipe_id), **extra})


@router.get("/widget")
def widget(request: Request, draft_id: int | None = None, recipe_id: int | None = None,
           conn: sqlite3.Connection = Depends(get_conn)):
    return render_widget(request, conn, *_owner({"draft_id": draft_id, "recipe_id": recipe_id}))


@router.get("/{cover_id}.jpg")
def image(cover_id: int, download: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
    c = svc.get_cover(conn, cover_id)
    path = svc.covers_dir() / c["file"] if c else None
    if not c or not path.exists():
        raise HTTPException(404, "No such cover")
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}  # files never change
    if download:
        title = conn.execute("SELECT title FROM drafts WHERE id = ?", (c["draft_id"],)).fetchone() if c["draft_id"] else None
        slug = re.sub(r"[^a-z0-9]+", "-", (title["title"] if title else "setlistarr-cover").lower()).strip("-")
        return FileResponse(path, media_type="image/jpeg", filename=f"{slug or 'cover'}-{cover_id}.jpg", headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers)


@router.post("/upload")
async def upload(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    draft_id, recipe_id = _owner(form)
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        return render_widget(request, conn, draft_id, recipe_id, error="Choose an image first")
    data = await f.read(svc.MAX_UPLOAD_BYTES + 1)
    try:
        cid = svc.store(conn, data, kind="upload", draft_id=draft_id, recipe_id=recipe_id)
    except svc.CoverError as e:
        return render_widget(request, conn, draft_id, recipe_id, error=str(e))
    svc.set_cover(conn, cid, draft_id=draft_id, recipe_id=recipe_id)
    return render_widget(request, conn, draft_id, recipe_id, notice="Cover uploaded.")


@router.post("/generate")
def generate(request: Request, prompt: str = Form(""), provider_id: str = Form(""), draft_id: str = Form(""),
             recipe_id: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    # sync def: FastAPI runs it in the threadpool, so a 30-60 s image call doesn't block the server
    did, rid = _owner({"draft_id": draft_id, "recipe_id": recipe_id})
    try:
        cid = svc.generate(conn, prompt=prompt, provider_id=int(provider_id) if provider_id.isdigit() else None,
                           draft_id=did, recipe_id=rid)
    except (svc.CoverError, ProviderError) as e:
        return render_widget(request, conn, did, rid, error=str(e))
    svc.set_cover(conn, cid, draft_id=did, recipe_id=rid)
    c = svc.get_cover(conn, cid)
    cost = c["cost_usd"]
    note = "free" if cost == 0 else ("cost unknown - add pricing for this model" if cost is None else f"${cost:.3f}")
    return render_widget(request, conn, did, rid, notice=f"New cover generated ({note}).")


@router.post("/select")
async def select(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    draft_id, recipe_id = _owner(form)
    cid = str(form.get("cover_id") or "")
    if cid == "none":
        svc.set_cover(conn, None, draft_id=draft_id, recipe_id=recipe_id)
    elif cid.isdigit() and svc.get_cover(conn, int(cid)):
        svc.set_cover(conn, int(cid), draft_id=draft_id, recipe_id=recipe_id)
    return render_widget(request, conn, draft_id, recipe_id)

