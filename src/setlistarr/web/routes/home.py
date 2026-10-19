from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from ...config import Settings
from ...llm.registry import cost_hint, text_providers
from ...services.generations import recent_generations
from ...services.recipes import list_recipes
from ..deps import get_conn, templates

router = APIRouter()


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    providers = text_providers(conn)
    lib = dict(conn.execute(
        "SELECT COALESCE(SUM(track_count), 0) AS tracks, MAX(last_sync_at) AS last_sync, "
        "COUNT(*) FILTER (WHERE enabled = 1) AS sections FROM libraries").fetchone())
    spend = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM (SELECT cost_usd, created_at FROM generations UNION ALL "
        "SELECT cost_usd, created_at FROM covers) WHERE created_at >= datetime('now', '-30 days')"
    ).fetchone()[0]
    next_runs = request.app.state.scheduler.recipe_next_runs()
    recipes = sorted(
        (r for r in list_recipes(conn) if r["enabled"]),
        key=lambda r: (next_runs.get(r["id"]) is None, str(next_runs.get(r["id"]) or "")),
    )[:5]
    setup = [
        ("Connect Plex and pick music libraries", Settings(conn).plex_configured() and lib["sections"] > 0, "/settings"),
        ("Sync the library", bool(lib["last_sync"]), "/system"),
        ("Add an AI provider", bool(providers), "/settings/providers"),
    ]
    return templates.TemplateResponse(
        request, "home.html",
        {"lib": lib, "providers": providers, "hints": {p.id: cost_hint(conn, p) for p in providers},
         "recent": recent_generations(conn, 6), "spend": spend, "recipes": recipes, "next_runs": next_runs,
         "managed_count": conn.execute("SELECT COUNT(*) FROM managed_playlists").fetchone()[0],
         "setup": setup, "setup_done": all(done for _, done, _ in setup)},
    )
