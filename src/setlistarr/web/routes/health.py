"""GET /health - liveness plus dependency status.

Returns 200 whenever the app and its DB work, even if Plex is down: a Plex outage
shouldn't make Docker restart-loop this container. Unauthenticated callers (Docker's
healthcheck, monitoring) only get {"status": ...}; details need a logged-in session.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ... import __version__
from ...config import Settings
from ...core.db import latest_version
from ...core.plex.client import PlexClient, PlexError
from ..auth import auth_disabled
from ..deps import get_conn

router = APIRouter()

_PLEX_CACHE_SEC = 60
_plex_cache: dict = {"at": 0.0, "key": None, "result": None}


def plex_status(settings: Settings) -> dict:
    url, token = settings.get("plex_url"), settings.get("plex_token")
    if not url or not token:
        return {"status": "unconfigured"}
    key = (url, token)
    if _plex_cache["key"] == key and time.monotonic() - _plex_cache["at"] < _PLEX_CACHE_SEC:
        return _plex_cache["result"]
    try:
        with PlexClient(url, token, timeout=3) as plex:
            ident = plex.identity()
        result = {"status": "ok", "server": ident.get("name"), "version": ident.get("version")}
    except PlexError as e:
        result = {"status": "error", "error": str(e)}
    _plex_cache.update(at=time.monotonic(), key=key, result=result)
    return result


@router.get("/health")
def health(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        last_sync = conn.execute("SELECT MAX(last_sync_at) FROM libraries").fetchone()[0]
        db = {"status": "ok" if schema == latest_version() else "migrating", "schema_version": schema}
    except sqlite3.Error as e:
        if not _detailed(request):
            return JSONResponse({"status": "error"}, 503)
        return JSONResponse({"status": "error", "db": {"status": "error", "error": str(e)}}, 503)

    plex = plex_status(Settings(conn))
    sched = getattr(request.app.state, "scheduler", None)
    body = {
        "status": "ok" if plex["status"] == "ok" else "degraded",
        "version": __version__,
        "db": db,
        "plex": plex,
        "scheduler": "running" if sched and sched.running else "stopped",
        "last_sync": last_sync,
    }
    if not _detailed(request):
        return JSONResponse({"status": body["status"]})
    return JSONResponse(body)


def _detailed(request: Request) -> bool:
    return auth_disabled() or bool(request.session.get("user"))
