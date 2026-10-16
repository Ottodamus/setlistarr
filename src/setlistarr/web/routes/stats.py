from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from ...services.library import library_stats
from ..deps import get_conn, templates

router = APIRouter()


@router.get("/stats")
def stats(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    s = library_stats(conn)
    max_decade = max((d["n"] for d in s["decades"]), default=1)
    return templates.TemplateResponse(request, "stats.html", {"s": s, "max_decade": max_decade})
