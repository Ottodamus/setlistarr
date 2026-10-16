from __future__ import annotations

import json
import sqlite3

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...config import Settings
from ...generation.constraints import Constraints
from ...llm.registry import list_providers
from ...services import recipes as svc
from ..deps import get_conn, templates

router = APIRouter(prefix="/recipes")
CONSTRAINT_FIELDS = ("target_minutes", "max_per_artist", "year_min", "year_max", "exclude_artists", "exclude_tags")


def _form_page(request, conn, recipe: dict | None, *, prefill: dict | None = None, error: str | None = None,
               status_code: int = 200):
    r = recipe or prefill or {}
    c = r.get("constraints") or Constraints()
    return templates.TemplateResponse(
        request, "recipe_form.html",
        {"r": r, "c": c, "providers": list_providers(conn), "presets": svc.PRESETS, "error": error},
        status_code=status_code,
    )


@router.get("")
def recipes_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    next_runs = request.app.state.scheduler.recipe_next_runs()
    return templates.TemplateResponse(
        request, "recipes.html", {"recipes": svc.list_recipes(conn), "next_runs": next_runs})


@router.get("/new")
def new_recipe(request: Request, draft_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    prefill = None
    if draft_id:
        row = conn.execute(
            """SELECT d.title, g.prompt, g.provider_id, g.mode_requested, g.constraints_json,
                      (SELECT p.plex_playlist_key FROM pushes p JOIN managed_playlists m
                         ON m.plex_playlist_key = p.plex_playlist_key
                       WHERE p.draft_id = d.id ORDER BY p.id DESC LIMIT 1) AS pushed_key
               FROM drafts d JOIN generations g ON g.id = d.generation_id WHERE d.id = ?""",
            (draft_id,),
        ).fetchone()
        if row:
            c = Constraints.from_dict(json.loads(row["constraints_json"] or "{}"))
            # keep only what the user pinned; the prompt re-derives the rest on every run
            for f in CONSTRAINT_FIELDS:
                if f not in c.locked:
                    setattr(c, f, [] if f.startswith("exclude") else None)
            prefill = {"name": row["title"], "prompt": row["prompt"], "provider_id": row["provider_id"],
                       "mode": None, "constraints": c, "playlist_title": row["title"],
                       "plex_playlist_key": row["pushed_key"], "schedule_cron": "0 6 * * 1", "enabled": 1,
                       "avoid_repeats": 1}
    return _form_page(request, conn, None, prefill=prefill)


@router.get("/{recipe_id}")
def edit_recipe(recipe_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    r = svc.get_recipe(conn, recipe_id)
    if not r:
        raise HTTPException(404, "No such recipe")
    return _form_page(request, conn, r)


@router.post("/save")
async def save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    rid = int(form["id"]) if str(form.get("id", "")).isdigit() else None
    s = Settings(conn)
    c = Constraints.from_form({k: form.get(k) for k in CONSTRAINT_FIELDS},
                              default_target=s.get("default_target_minutes"),
                              default_cap=s.get("default_max_per_artist"))
    cron = str(form.get("schedule_cron_custom") or form.get("schedule_cron") or "").strip()
    data = {
        "name": form.get("name"), "prompt": form.get("prompt"),
        "provider_id": int(form["provider_id"]) if str(form.get("provider_id", "")).isdigit() else None,
        "mode": form.get("mode") or None, "constraints": c, "schedule_cron": cron,
        "enabled": "enabled" in form, "avoid_repeats": "avoid_repeats" in form,
        "playlist_title": form.get("playlist_title"), "plex_playlist_key": form.get("plex_playlist_key") or None,
    }
    error = None
    if cron:
        try:
            CronTrigger.from_crontab(cron)
        except ValueError as e:
            error = f"Schedule isn't a valid cron expression: {e}"
    if not error:
        try:
            rid = svc.save_recipe(conn, data, rid)
        except sqlite3.IntegrityError:
            error = f"A recipe named {data['name']!r} already exists"
        except ValueError as e:
            error = str(e)
    if error:
        return _form_page(request, conn, {**data, "id": rid} if rid else None, prefill=data, error=error,
                          status_code=400)
    request.app.state.scheduler.reschedule()
    return RedirectResponse("/recipes", status_code=303)


@router.post("/{recipe_id}/run")
def run_now(recipe_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if not svc.get_recipe(conn, recipe_id):
        raise HTTPException(404, "No such recipe")
    request.app.state.scheduler.trigger_recipe(recipe_id)
    conn.execute("UPDATE recipes SET last_status = 'running', last_error = NULL WHERE id = ?", (recipe_id,))
    conn.commit()
    return RedirectResponse("/recipes", status_code=303)


@router.post("/{recipe_id}/toggle")
def toggle(recipe_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("UPDATE recipes SET enabled = 1 - enabled WHERE id = ?", (recipe_id,))
    conn.commit()
    request.app.state.scheduler.reschedule()
    return RedirectResponse("/recipes", status_code=303)


@router.post("/{recipe_id}/delete")
def delete(recipe_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    svc.delete_recipe(conn, recipe_id)
    request.app.state.scheduler.reschedule()
    return RedirectResponse("/recipes", status_code=303)
