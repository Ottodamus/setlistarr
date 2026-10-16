"""Recipes: saved prompts that regenerate and replace their own Plex playlist.

A run = generation (synchronous, in the scheduler's thread) -> push. The playlist is only
touched if the generation succeeded; the first successful run creates it, later runs
replace it in place. Recipe runs don't take the library lock - a long LLM call must not
block syncs - but two runs of the same recipe never overlap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from ..config import bootstrap
from ..core.db import connect
from ..core.plex.client import PlexError
from ..generation.constraints import Constraints
from ..generation.runner import manager
from ..llm.base import ProviderError
from ..logs import get_logger
from .generations import get_generation
from .push import push_draft

log = get_logger(__name__)

_running: set[int] = set()
_running_lock = threading.Lock()

PRESETS = {
    "": "Manual only",
    "0 6 * * *": "Daily at 6:00",
    "0 6 * * 1": "Weekly, Monday 6:00",
    "0 6 * * 1,4": "Twice a week (Mon & Thu 6:00)",
    "0 6 1 * *": "Monthly, 1st at 6:00",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe_schedule(cron: str | None) -> str:
    cron = (cron or "").strip()
    return PRESETS.get(cron, f"cron: {cron}")


def list_recipes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.*, p.name AS provider_name, p.model AS provider_model, m.title AS playlist_current_title,
                  m.track_count AS playlist_tracks
           FROM recipes r LEFT JOIN providers p ON p.id = r.provider_id
           LEFT JOIN managed_playlists m ON m.plex_playlist_key = r.plex_playlist_key
           ORDER BY r.name"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["constraints"] = Constraints.from_dict(json.loads(d.pop("constraints_json") or "{}"))
        d["schedule_text"] = describe_schedule(d["schedule_cron"])
        d["running"] = d["id"] in _running
        out.append(d)
    return out


def get_recipe(conn: sqlite3.Connection, recipe_id: int) -> dict[str, Any] | None:
    return next((r for r in list_recipes(conn) if r["id"] == recipe_id), None)


def save_recipe(conn: sqlite3.Connection, data: dict[str, Any], recipe_id: int | None = None) -> int:
    name = (data.get("name") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    if not name or not prompt:
        raise ValueError("A recipe needs a name and a prompt")
    c: Constraints = data["constraints"]
    values = (
        name, prompt, data.get("provider_id"), data.get("mode") or None, json.dumps(c.to_dict()),
        (data.get("schedule_cron") or "").strip() or None, int(bool(data.get("enabled", True))),
        int(bool(data.get("avoid_repeats", True))), (data.get("playlist_title") or "").strip() or None,
    )
    now = _now()
    with conn:
        if recipe_id:
            conn.execute(
                "UPDATE recipes SET name=?, prompt=?, provider_id=?, mode=?, constraints_json=?, schedule_cron=?, "
                "enabled=?, avoid_repeats=?, playlist_title=?, updated_at=? WHERE id=?",
                (*values, now, recipe_id),
            )
            return recipe_id
        rid = conn.execute(
            "INSERT INTO recipes(name, prompt, provider_id, mode, constraints_json, schedule_cron, enabled, "
            "avoid_repeats, playlist_title, plex_playlist_key, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, data.get("plex_playlist_key"), now, now),
        ).lastrowid
        if data.get("plex_playlist_key"):
            conn.execute("UPDATE managed_playlists SET recipe_id = ? WHERE plex_playlist_key = ?",
                         (rid, data["plex_playlist_key"]))
        return rid


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> None:
    """Deletes the recipe only. Its Plex playlist stays (and stays managed)."""
    with conn:
        conn.execute("UPDATE managed_playlists SET recipe_id = NULL WHERE recipe_id = ?", (recipe_id,))
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))


def _previous_keys(conn: sqlite3.Connection, generation_id: int | None) -> list[str]:
    if not generation_id:
        return []
    return [r[0] for r in conn.execute(
        "SELECT dt.plex_key FROM draft_tracks dt JOIN drafts d ON d.id = dt.draft_id WHERE d.generation_id = ? "
        "ORDER BY dt.position", (generation_id,))]


def run_recipe(recipe_id: int, trigger: str = "manual", *, inject: dict | None = None) -> dict[str, Any]:
    """Generate + push. Never raises; the outcome is recorded on the recipe and in jobs."""
    with _running_lock:
        if recipe_id in _running:
            return {"status": "skipped", "error": "already running"}
        _running.add(recipe_id)
    conn = connect(bootstrap().db_path)
    job_id = None
    try:
        r = get_recipe(conn, recipe_id)
        if r is None:
            return {"status": "error", "error": "recipe not found"}
        job_id = conn.execute(
            "INSERT INTO jobs(kind, trigger, started_at, status) VALUES('recipe', ?, ?, 'running')", (trigger, _now())
        ).lastrowid
        conn.execute("UPDATE recipes SET last_status='running', last_error=NULL, last_run_at=? WHERE id=?",
                     (_now(), recipe_id))
        conn.commit()
        log.info("recipe.start", recipe_id=recipe_id, name=r["name"], trigger=trigger)

        status, error, result = "ok", None, {}
        try:
            c: Constraints = r["constraints"]
            if r["avoid_repeats"]:
                c.exclude_keys = _previous_keys(conn, r["last_generation_id"])
            gen_id = manager.start(conn, r["prompt"], provider_id=r["provider_id"], constraints=c, mode=r["mode"],
                                   background=False, recipe_id=recipe_id, inject=inject)
            conn.execute("UPDATE recipes SET last_generation_id = ? WHERE id = ?", (gen_id, recipe_id))
            conn.commit()
            g = get_generation(conn, gen_id)
            result["generation_id"] = gen_id
            if g["status"] != "ok":
                raise ProviderError(g["error"] or f"generation {g['status']}")
            managed = r["plex_playlist_key"] and conn.execute(
                "SELECT 1 FROM managed_playlists WHERE plex_playlist_key = ?", (r["plex_playlist_key"],)).fetchone()
            draft = conn.execute("SELECT description FROM drafts WHERE id = ?", (g["draft_id"],)).fetchone()
            push = push_draft(conn, g["draft_id"], title=r["playlist_title"] or r["name"],
                              summary=draft["description"] if draft else None,
                              replace_key=r["plex_playlist_key"] if managed else None)
            with conn:
                conn.execute("UPDATE recipes SET plex_playlist_key = ? WHERE id = ?", (push["ratingKey"], recipe_id))
                conn.execute("UPDATE managed_playlists SET recipe_id = ? WHERE plex_playlist_key = ?",
                             (recipe_id, push["ratingKey"]))
            result.update(action=push["action"], tracks=push["track_count"], playlist=push["title"])
        except (ProviderError, PlexError, ValueError) as e:
            status, error = "error", str(e)
        except Exception as e:  # never kill the scheduler thread
            status, error = "error", f"{type(e).__name__}: {e}"
            log.exception("recipe.failed", recipe_id=recipe_id)

        with conn:
            conn.execute("UPDATE recipes SET last_status=?, last_error=? WHERE id=?", (status, error, recipe_id))
            conn.execute("UPDATE jobs SET finished_at=?, status=?, stats_json=?, error=? WHERE id=?",
                         (_now(), status, json.dumps({r["name"]: result}), error, job_id))
        log.info("recipe.finish", recipe_id=recipe_id, status=status, error=error, **result)
        return {"status": status, "error": error, **result}
    finally:
        conn.close()
        with _running_lock:
            _running.discard(recipe_id)
