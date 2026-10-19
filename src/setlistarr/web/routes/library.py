from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from ...services.library import TRACK_SORTS, browse_artists, browse_tracks, library_stats, playlist_choices
from ..deps import get_conn, is_htmx, templates

router = APIRouter()


@router.get("/library")
def tracks(
    request: Request,
    q: str = "",
    tag: str = "",
    decade: str = "",
    sort: str = "artist",
    page: int = 1,
    playlist: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    dec = int(decade) if decade.isdigit() else None
    result = browse_tracks(conn, q=q, tag=tag, decade=dec, sort=sort, page=page, playlist=playlist)
    ctx = {"result": result, "q": q, "tag": tag, "decade": dec, "sort": sort, "sorts": list(TRACK_SORTS),
           "playlist": playlist}
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/tracks_table.html", ctx)
    stats = library_stats(conn)
    ctx.update(
        decades=[d["decade"] for d in stats["decades"] if d["decade"]],
        configured=bool(stats["libraries"]),
        total_tracks=stats["totals"]["tracks"],
        playlists=playlist_choices(conn),
    )
    return templates.TemplateResponse(request, "library_tracks.html", ctx)


@router.get("/library/artists")
def artists(
    request: Request, q: str = "", page: int = 1, conn: sqlite3.Connection = Depends(get_conn)
):
    result = browse_artists(conn, q=q, page=page)
    ctx = {"result": result, "q": q}
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/artists_table.html", ctx)
    return templates.TemplateResponse(request, "library_artists.html", ctx)
