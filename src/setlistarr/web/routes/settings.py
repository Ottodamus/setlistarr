from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ...config import SETTING_DEFS, Settings
from ...core.plex.client import PlexClient, PlexError
from ...core.plex.sync import refresh_sections
from ..deps import get_conn, templates

router = APIRouter()

APPEARANCE_KEYS = ["ui_theme"]
PLEX_KEYS = ["plex_url", "plex_token", "plex_timeout_sec"]
SYNC_KEYS = ["sync_interval_minutes", "full_sync_cron"]
ENRICH_KEYS = ["lastfm_api_key", "musicbrainz_contact", "enrich_after_full_sync", "enrich_track_tags"]
GENERATION_KEYS = ["default_target_minutes", "default_max_per_artist"]


def _fields(s: Settings, keys: list[str]) -> list[dict]:
    out = []
    for k in keys:
        d = SETTING_DEFS[k]
        val = s.get(k)
        out.append(
            {
                "key": k,
                "label": d.label,
                "help": d.help,
                "kind": d.kind.__name__,
                "secret": d.secret,
                "value": "" if d.secret else ("" if val is None else val),
                "is_set": val not in (None, ""),
                "choices": d.choices,
                "help_on": d.help_on, "help_off": d.help_off,
                "source": s.source(k),
                "env": d.env,
            }
        )
    return out


def _libraries(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM libraries ORDER BY name")]


@router.get("/settings")
def settings_page(request: Request, saved: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
    s = Settings(conn)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "groups": [
                ("Appearance", _fields(s, APPEARANCE_KEYS)),
                ("Plex", _fields(s, PLEX_KEYS)),
                ("Sync schedule", _fields(s, SYNC_KEYS)),
                ("Enrichment", _fields(s, ENRICH_KEYS)),
                ("Generation defaults", _fields(s, GENERATION_KEYS)),
            ],
            "libraries": _libraries(conn),
            "saved": saved,
        },
    )


@router.post("/settings")
async def save_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    s = Settings(conn)
    for key in APPEARANCE_KEYS + PLEX_KEYS + SYNC_KEYS + ENRICH_KEYS + GENERATION_KEYS:
        d = SETTING_DEFS[key]
        if s.source(key) == "env":
            continue
        if d.kind is bool:
            s.set(key, key in form)
            continue
        raw = str(form.get(key, "")).strip()
        if d.secret and not raw:
            continue  # blank secret field = keep the current value
        if key == "plex_url":
            raw = raw.rstrip("/")
        s.set(key, raw)
    request.app.state.scheduler.reschedule()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/plex/test")
async def test_plex(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Test the URL/token in the form (falling back to saved values for blank secrets)
    and, on success, refresh the list of music sections."""
    form = await request.form()
    s = Settings(conn)
    url = str(form.get("plex_url") or s.get("plex_url") or "").strip().rstrip("/")
    token = str(form.get("plex_token") or s.get("plex_token") or "").strip()
    ctx: dict = {"ok": False}
    if not url or not token:
        ctx["error"] = "Enter a server URL and token first."
    else:
        try:
            with PlexClient(url, token, timeout=5) as plex:
                ident = plex.identity()
                if s.source("plex_url") != "env":
                    s.set("plex_url", url)
                if s.source("plex_token") != "env":
                    s.set("plex_token", token)
                refresh_sections(conn, plex)
            ctx = {"ok": True, "ident": ident}
        except PlexError as e:
            ctx["error"] = str(e)
    ctx["libraries"] = _libraries(conn)
    return templates.TemplateResponse(request, "partials/plex_test.html", ctx)


@router.post("/settings/libraries")
async def save_libraries(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    enabled = {int(v) for v in form.getlist("enabled") if str(v).isdigit()}
    for lib in _libraries(conn):
        conn.execute(
            "UPDATE libraries SET enabled = ? WHERE section_id = ?",
            (1 if lib["section_id"] in enabled else 0, lib["section_id"]),
        )
    conn.commit()
    return templates.TemplateResponse(
        request, "partials/libraries.html", {"libraries": _libraries(conn), "saved": True}
    )
