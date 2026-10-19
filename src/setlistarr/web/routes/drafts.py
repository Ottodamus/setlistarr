"""Draft editor (HTMX): every mutation returns the re-rendered editor partial."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ...core.plex.client import PlexError
from ...generation.constraints import Constraints
from ...generation.runner import manager
from ...llm.base import ProviderError
from ...llm.registry import text_providers
from ...services import drafts as svc
from ...services.push import managed_playlists, push_draft
from ..deps import get_conn, templates

router = APIRouter(prefix="/drafts")


def render_editor(request: Request, conn: sqlite3.Connection, draft_id: int, **extra):
    d = svc.load_for_editor(conn, draft_id)
    if not d:
        raise HTTPException(404, "No such draft")
    managed = managed_playlists(conn)
    last_push = d["pushes"][0]["plex_playlist_key"] if d["pushes"] else None
    managed_keys = {m["plex_playlist_key"] for m in managed}
    return templates.TemplateResponse(
        request, "partials/draft_editor.html",
        {"d": d, "managed": managed, "default_target": last_push if last_push in managed_keys else "new",
         "providers": text_providers(conn), **extra},
    )


async def _form(request: Request) -> dict:
    form = await request.form()
    return {k: form.getlist(k) if k == "key" else form.get(k) for k in form.keys()}


@router.get("/{draft_id}")
def draft_page(draft_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT generation_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No such draft")
    return RedirectResponse(f"/generate/{row['generation_id']}#draft", status_code=302)


@router.get("/{draft_id}/editor")
def editor(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render_editor(request, conn, draft_id)


def _mutate(request, conn, draft_id, fn, *args):
    try:
        fn(conn, draft_id, *args)
    except svc.DraftError as e:
        return render_editor(request, conn, draft_id, error=str(e))
    return render_editor(request, conn, draft_id)


@router.post("/{draft_id}/remove")
async def remove(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = await _form(request)
    return _mutate(request, conn, draft_id, svc.remove_track, str(f.get("plex_key", "")))


@router.post("/{draft_id}/lock")
async def lock(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = await _form(request)
    return _mutate(request, conn, draft_id, svc.toggle_lock, str(f.get("plex_key", "")))


@router.post("/{draft_id}/reorder")
async def reorder(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = await _form(request)
    return _mutate(request, conn, draft_id, svc.reorder, f.get("key") or [])


@router.post("/{draft_id}/add")
async def add(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = await _form(request)
    return _mutate(request, conn, draft_id, svc.add_track, str(f.get("plex_key", "")))


@router.post("/{draft_id}/meta")
async def meta(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = await _form(request)
    resp = _mutate(request, conn, draft_id, svc.update_meta, str(f.get("title", "")), str(f.get("description", "")))
    return resp


@router.get("/{draft_id}/search")
def search(draft_id: int, request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "partials/draft_search.html",
        {"rows": svc.search_library(conn, draft_id, q), "draft_id": draft_id, "q": q},
    )


@router.post("/{draft_id}/push")
async def push(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = await _form(request)
    target = str(f.get("target") or "new")
    try:
        res = push_draft(conn, draft_id, title=str(f.get("title", "")), summary=f.get("summary") or None,
                         replace_key=None if target == "new" else target)
    except PlexError as e:
        return render_editor(request, conn, draft_id, error=f"Push failed: {e}")
    return render_editor(request, conn, draft_id, pushed=res)


@router.post("/{draft_id}/regenerate")
async def regenerate(draft_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """New generation keeping this draft's locked tracks; the original draft is left as-is."""
    f = await _form(request)
    d = svc.load_for_editor(conn, draft_id)
    if not d:
        raise HTTPException(404, "No such draft")
    g = d["generation"]
    prompt = str(f.get("prompt") or g.get("prompt") or "").strip()
    constraints = Constraints.from_dict(json.loads(g.get("constraints_json") or "{}"))
    # The model's own constraint guesses get re-derived from the (possibly edited) prompt.
    for field in ("target_minutes", "max_per_artist", "exclude_artists", "exclude_tags", "year_min", "year_max"):
        if field not in constraints.locked:
            setattr(constraints, field, [] if field.startswith("exclude") else None)
    provider_id = int(f["provider_id"]) if str(f.get("provider_id", "")).isdigit() else g.get("provider_id")
    seed = [t["plex_key"] for t in d["tracks"] if t["locked"] and not t["missing"]]
    try:
        gen_id = manager.start(conn, prompt, provider_id=provider_id, constraints=constraints,
                               seed_keys=seed, parent_draft_id=draft_id)
    except (ProviderError, ValueError) as e:
        return render_editor(request, conn, draft_id, error=str(e))
    return Response(status_code=204, headers={"HX-Redirect": f"/generate/{gen_id}"})
