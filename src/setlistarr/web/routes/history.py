from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from ...config import Settings
from ...services.push import managed_playlists
from ..deps import get_conn, templates

router = APIRouter()
PER_PAGE = 30


@router.get("/history")
def history(request: Request, page: int = 1, status: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    page = max(1, page)
    where, params = "", []
    if status:
        where, params = "WHERE g.status = ?", [status]
    total = conn.execute(f"SELECT COUNT(*) FROM generations g {where}", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"""SELECT g.*, d.id AS draft_id, d.title AS draft_title, d.status AS draft_status,
                   (SELECT COUNT(*) FROM draft_tracks dt WHERE dt.draft_id = d.id) AS tracks,
                   (SELECT p.title FROM pushes p WHERE p.draft_id = d.id ORDER BY p.id DESC LIMIT 1) AS pushed_to
            FROM generations g LEFT JOIN drafts d ON d.generation_id = g.id
            {where} ORDER BY g.id DESC LIMIT ? OFFSET ?""",
        [*params, PER_PAGE, (page - 1) * PER_PAGE],
    )]
    spend = dict(conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0) AS all_time,
                  COALESCE(SUM(CASE WHEN created_at >= datetime('now', '-30 days') THEN cost_usd END), 0) AS last_30d,
                  SUM(cost_usd IS NULL AND status = 'ok') AS unpriced
           FROM (SELECT cost_usd, created_at, status FROM generations
                 UNION ALL SELECT cost_usd, created_at, 'ok' FROM covers WHERE kind != 'upload')"""
    ).fetchone())
    plex_url = Settings(conn).get("plex_url") or ""
    return templates.TemplateResponse(
        request, "history.html",
        {"rows": rows, "page": page, "pages": max(1, -(-total // PER_PAGE)), "status": status, "spend": spend,
         "managed": managed_playlists(conn), "plex_url": plex_url.rstrip("/")},
    )
