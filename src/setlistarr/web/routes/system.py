from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from ...config import Settings
from ...scheduler import JOBS
from ...services.jobs import library_busy, recent_jobs
from ..deps import get_conn, is_htmx, templates
from .health import plex_status

router = APIRouter()


def _ctx(request: Request, conn) -> dict:
    jobs = recent_jobs(conn)
    return {
        "jobs": jobs,
        "busy": library_busy() or any(j["status"] == "running" for j in jobs),
        "next_runs": request.app.state.scheduler.next_runs(),
    }


@router.get("/system")
def system_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ctx = _ctx(request, conn)
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/jobs_table.html", ctx)
    ctx.update(plex=plex_status(Settings(conn)), libraries=[
        dict(r) for r in conn.execute("SELECT * FROM libraries ORDER BY name")
    ])
    return templates.TemplateResponse(request, "system.html", ctx)


@router.post("/system/jobs/{kind}")
def trigger_job(kind: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if kind not in JOBS:
        raise HTTPException(404, f"unknown job {kind}")
    if kind.startswith("sync") and not Settings(conn).plex_configured():
        return templates.TemplateResponse(
            request, "partials/alert.html",
            {"level": "error", "message": "Configure Plex in Settings first."},
        )
    request.app.state.scheduler.trigger(kind)
    ctx = _ctx(request, conn)
    ctx["busy"] = True  # the job may not have inserted its row yet; keep polling
    return templates.TemplateResponse(request, "partials/jobs_table.html", ctx)
