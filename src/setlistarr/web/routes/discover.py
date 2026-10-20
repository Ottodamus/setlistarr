"""Discover page + Settings -> Discover (Lidarr, ListenBrainz, schedule)."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ...config import SETTING_DEFS, Settings
from ...core.db import get_meta
from ...core.lidarr import LidarrClient, LidarrError
from ...core.listenbrainz import ListenBrainzClient, ListenBrainzError
from ...core.plex.client import PlexError, client_from_settings
from ...services import discover as svc
from ...services.covers import cover_providers
from ..deps import get_conn, templates

router = APIRouter()

SETTINGS_KEYS = ["discover_source", "discover_cadence", "discover_auto_approve", "discover_per_run",
                 "discover_minutes", "discover_window_weeks", "discover_playlist_title", "discover_cover_mode",
                 "discover_cover_provider_id", "lidarr_url", "lidarr_api_key", "lidarr_root_folder",
                 "lidarr_quality_profile_id", "lidarr_metadata_profile_id", "listenbrainz_token",
                 "scrobble_enabled", "scrobble_plex_account_id"]


def _rows(conn, status: str, limit: int = 200) -> list[dict]:
    out = []
    order = "score DESC" if status == "suggested" else "updated_at DESC"
    for r in conn.execute(f"SELECT * FROM discoveries WHERE status = ? ORDER BY {order} LIMIT ?", (status, limit)):
        d = dict(r)
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        out.append(d)
    return out


def _page_ctx(request: Request, conn) -> dict:
    s = Settings(conn)
    runs = [dict(r) for r in conn.execute("SELECT * FROM discover_runs ORDER BY id DESC LIMIT 5")]
    for r in runs:
        r["stats"] = json.loads(r.pop("stats_json") or "{}")
    key = get_meta(conn, "discover_playlist_key")
    playlist = conn.execute("SELECT * FROM managed_playlists WHERE plex_playlist_key = ?", (key,)).fetchone() if key else None
    nxt = next((j.next_run_time for j in request.app.state.scheduler._sched.get_jobs()
                if j.id == "periodic-discover" and getattr(j, "next_run_time", None)), None)
    return {
        "s": {k: s.get(k) for k in SETTINGS_KEYS if k not in ("lidarr_api_key", "listenbrainz_token")},
        "lidarr_ready": bool(s.get("lidarr_url") and s.get("lidarr_api_key") and s.get("lidarr_root_folder")
                             and s.get("lidarr_quality_profile_id") and s.get("lidarr_metadata_profile_id")),
        "lb_ready": bool(s.get("listenbrainz_token") and s.get("listenbrainz_user")),
        "lastfm_ready": bool(s.get("lastfm_api_key")),
        "suggested": _rows(conn, "suggested"), "requested": _rows(conn, "requested"),
        "arrived": _rows(conn, "arrived"), "failed": _rows(conn, "failed"), "dismissed": _rows(conn, "dismissed", 50),
        "runs": runs, "running": any(r["status"] == "running" for r in runs),
        "playlist": dict(playlist) if playlist else None, "playlist_title": svc.playlist_title(s),
        "draft_id": get_meta(conn, "discover_draft_id"), "next_run": nxt,
    }


@router.get("/discover")
def discover_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(request, "discover.html", _page_ctx(request, conn))


def _lists(request, conn, **extra):
    return templates.TemplateResponse(request, "partials/discover_lists.html", {**_page_ctx(request, conn), **extra})


@router.post("/discover/{discovery_id}/approve")
def approve(discovery_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        res = svc.request(conn, [discovery_id])
    except (svc.DiscoverError, LidarrError) as e:
        return _lists(request, conn, error=str(e))
    r = res[0] if res else None
    msg = (f"Sent {r['artist']} to Lidarr" + (f" - album: {r['album_title']}" if r.get("album_title") else "")
           if r and r["status"] == "requested" else (f"{r['artist']}: {r['note']}" if r else None))
    return _lists(request, conn, notice=msg if r and r["status"] == "requested" else None,
                  error=msg if r and r["status"] == "failed" else None)


@router.post("/discover/approve-all")
def approve_all(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ids = [r["id"] for r in conn.execute("SELECT id FROM discoveries WHERE status = 'suggested' ORDER BY score DESC")]
    try:
        res = svc.request(conn, ids)
    except (svc.DiscoverError, LidarrError) as e:
        return _lists(request, conn, error=str(e))
    ok = sum(1 for r in res if r["status"] == "requested")
    bad = [f"{r['artist']}: {r['note']}" for r in res if r["status"] == "failed"]
    return _lists(request, conn, notice=f"Sent {ok} artist{'s' if ok != 1 else ''} to Lidarr." if ok else None,
                  error="; ".join(bad) if bad else None)


@router.post("/discover/{discovery_id}/dismiss")
def dismiss(discovery_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    svc.dismiss(conn, discovery_id)
    return _lists(request, conn)


@router.post("/discover/{discovery_id}/undo")
def undo(discovery_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    svc.dismiss(conn, discovery_id, undo=True)
    return _lists(request, conn)


@router.post("/discover/run")
def run_now(request: Request):
    request.app.state.scheduler.trigger_discover()
    return RedirectResponse("/discover?started=1", status_code=303)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def _settings_ctx(conn, **extra) -> dict:
    s = Settings(conn)
    fields = {}
    for k in SETTINGS_KEYS:
        d = SETTING_DEFS[k]
        v = s.get(k)
        fields[k] = {"key": k, "label": d.label, "help": d.help, "choices": d.choices, "kind": d.kind.__name__,
                     "secret": d.secret, "value": "" if d.secret else ("" if v is None else v),
                     "is_set": v not in (None, ""), "source": s.source(k), "env": d.env}
    accounts = []
    try:
        with client_from_settings(s) as plex:
            accounts = [(int(a["id"]), a.get("name") or f"account {a['id']}") for a in plex.get("/accounts").get("Account", [])]
    except (PlexError, KeyError, ValueError):
        pass
    return {"f": fields, "cover_providers": cover_providers(conn), "accounts": accounts,
            "lb_user": s.get("listenbrainz_user"), **extra}


@router.get("/settings/discover")
def settings_page(request: Request, saved: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(request, "discover_settings.html", _settings_ctx(conn, saved=saved))


@router.post("/settings/discover")
async def save_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    s = Settings(conn)
    for key in SETTINGS_KEYS:
        d = SETTING_DEFS[key]
        if s.source(key) == "env":
            continue
        if d.kind is bool:
            s.set(key, key in form)
            continue
        raw = str(form.get(key, "")).strip()
        if d.secret and not raw:
            continue
        if key == "lidarr_url":
            raw = raw.rstrip("/")
        s.set(key, raw)
    request.app.state.scheduler.reschedule()
    return RedirectResponse("/settings/discover?saved=1", status_code=303)


@router.post("/settings/discover/lidarr-test")
async def lidarr_test(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    s = Settings(conn)
    url = str(form.get("lidarr_url") or s.get("lidarr_url") or "").strip().rstrip("/")
    key = str(form.get("lidarr_api_key") or s.get("lidarr_api_key") or "").strip()
    ctx: dict = {"ok": False, "s": {k: s.get(k) for k in ("lidarr_root_folder", "lidarr_quality_profile_id",
                                                          "lidarr_metadata_profile_id")}}
    if not url or not key:
        ctx["error"] = "Enter the Lidarr URL and API key first."
    else:
        try:
            with LidarrClient(url, key, timeout=10) as lidarr:
                status = lidarr.status()
                ctx.update(ok=True, version=status.get("version"), roots=lidarr.root_folders(),
                           qps=lidarr.quality_profiles(), mps=lidarr.metadata_profiles())
            if s.source("lidarr_url") != "env":
                s.set("lidarr_url", url)
            if s.source("lidarr_api_key") != "env":
                s.set("lidarr_api_key", key)
        except LidarrError as e:
            ctx["error"] = str(e)
    return templates.TemplateResponse(request, "partials/lidarr_test.html", ctx)


@router.post("/settings/discover/listenbrainz-test")
async def lb_test(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    s = Settings(conn)
    token = str(form.get("listenbrainz_token") or s.get("listenbrainz_token") or "").strip()
    ctx: dict = {"ok": False}
    try:
        with ListenBrainzClient(token) as lb:
            user = lb.validate_token()
        if s.source("listenbrainz_token") != "env":
            s.set("listenbrainz_token", token)
        s.set("listenbrainz_user", user)
        ctx.update(ok=True, user=user)
    except ListenBrainzError as e:
        ctx["error"] = str(e)
    return templates.TemplateResponse(request, "partials/alert.html",
                                      {"level": "ok" if ctx["ok"] else "error",
                                       "message": f"Connected as {ctx['user']}." if ctx["ok"] else ctx["error"]})

