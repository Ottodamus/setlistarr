from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ...config import Settings
from ...generation.constraints import Constraints
from ...generation.runner import manager
from ...llm.base import ProviderError
from ...llm.registry import cost_hint, text_providers
from ...services.generations import get_generation, load_draft, recent_generations
from ..deps import get_conn, templates

router = APIRouter()

CONSTRAINT_FIELDS = ("target_minutes", "max_per_artist", "year_min", "year_max", "exclude_artists", "exclude_tags")


def _page(request, conn, *, error=None, form=None, status_code=200):
    providers = text_providers(conn)
    return templates.TemplateResponse(
        request,
        "generate.html",
        {
            "providers": providers,
            "hints": {p.id: cost_hint(conn, p) for p in providers},
            "recent": recent_generations(conn, 15),
            "library_tracks": conn.execute("SELECT COALESCE(SUM(track_count), 0) FROM libraries WHERE enabled = 1").fetchone()[0],
            "error": error,
            "form": form or {},
        },
        status_code=status_code,
    )


@router.get("/generate")
def generate_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn)


@router.post("/generate")
async def start_generation(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = dict(await request.form())
    s = Settings(conn)
    constraints = Constraints.from_form(
        {k: form.get(k) for k in CONSTRAINT_FIELDS},
        default_target=s.get("default_target_minutes"),
        default_cap=s.get("default_max_per_artist"),
    )
    provider_id = int(form["provider_id"]) if str(form.get("provider_id", "")).isdigit() else None
    mode = form.get("mode") or None
    try:
        gen_id = manager.start(conn, str(form.get("prompt", "")), provider_id=provider_id,
                               constraints=constraints, mode=mode)
    except (ProviderError, ValueError) as e:
        return _page(request, conn, error=str(e), form=form, status_code=400)
    return RedirectResponse(f"/generate/{gen_id}", status_code=303)


@router.get("/generate/{gen_id}")
def generation_page(gen_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    g = get_generation(conn, gen_id)
    if not g:
        raise HTTPException(404, "No such generation")
    live = g["status"] == "running" and manager.events(gen_id) is not None
    return templates.TemplateResponse(
        request, "generation.html",
        {"g": g, "live": live, "draft": load_draft(conn, g["draft_id"]) if g["draft_id"] else None},
    )


@router.get("/generate/{gen_id}/result")
def generation_result(gen_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    g = get_generation(conn, gen_id)
    if not g:
        raise HTTPException(404, "No such generation")
    return templates.TemplateResponse(
        request, "partials/generation_result.html",
        {"g": g, "draft": load_draft(conn, g["draft_id"]) if g["draft_id"] else None},
    )


@router.post("/generate/{gen_id}/cancel")
def cancel_generation(gen_id: int):
    manager.cancel(gen_id)
    return HTMLResponse('<span class="badge warn">cancelling…</span>')


def _sse(event: str, data: str, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines += [f"data: {line}" for line in (data.splitlines() or [""])]
    return "\n".join(lines) + "\n\n"


@router.get("/generate/{gen_id}/events")
async def generation_events(gen_id: int, request: Request):
    log = manager.events(gen_id)
    tmpl = templates.get_template("partials/gen_event.html")

    async def stream():
        if log is None:  # finished long ago or app restarted - the page reloads the result
            yield _sse("done", "")
            return
        seq = int(request.headers.get("last-event-id") or 0)
        while True:
            if await request.is_disconnected():
                return
            events, done = await asyncio.to_thread(log.wait_after, seq, 15)
            for e in events:
                seq = e["seq"]
                yield _sse("step", tmpl.render(e=e), seq)
            if done and seq >= len(log.events):
                yield _sse("done", "")
                return
            if not events:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
