"""Push a draft to Plex. Creates a new playlist, or replaces a *managed* one in place.

Safety rule: a playlist is only ever modified if its ratingKey is in managed_playlists,
i.e. Setlistarr created it. Nothing is matched or deleted by title.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..core.plex.client import PlexClient, PlexError, PlexNotFound, client_from_settings
from ..core.plex.playlists import create_playlist, replace_playlist, web_url
from ..generation.draft import fetch_tracks
from ..logs import get_logger

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def managed_playlists(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM managed_playlists ORDER BY updated_at DESC")]


def register_managed(conn: sqlite3.Connection, rating_key: str, title: str, track_count: int,
                     draft_id: int | None = None, recipe_id: int | None = None) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO managed_playlists(plex_playlist_key, title, track_count, last_draft_id, recipe_id, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(plex_playlist_key) DO UPDATE SET title=excluded.title, "
        "track_count=excluded.track_count, last_draft_id=COALESCE(excluded.last_draft_id, last_draft_id), "
        "recipe_id=COALESCE(excluded.recipe_id, recipe_id), updated_at=excluded.updated_at",
        (rating_key, title, track_count, draft_id, recipe_id, now, now),
    )


def push_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    title: str,
    summary: str | None = None,
    replace_key: str | None = None,
    plex: PlexClient | None = None,
) -> dict[str, Any]:
    """replace_key=None creates a new playlist; otherwise it must be a managed playlist."""
    title = title.strip()
    if not title:
        raise PlexError("A playlist title is required")
    keys = [r["plex_key"] for r in conn.execute(
        "SELECT plex_key FROM draft_tracks WHERE draft_id = ? ORDER BY position", (draft_id,))]
    live = fetch_tracks(conn, keys)
    push_keys = [k for k in keys if k in live]
    skipped = [k for k in keys if k not in live]
    if not push_keys:
        raise PlexError("None of the draft's tracks are in the library any more")
    if replace_key and not conn.execute(
        "SELECT 1 FROM managed_playlists WHERE plex_playlist_key = ?", (replace_key,)
    ).fetchone():
        raise PlexError("Setlistarr only replaces playlists it created - pick one from the list or create a new one")

    settings = Settings(conn)
    own = plex is None
    plex = plex or client_from_settings(settings)
    try:
        if replace_key:
            try:
                res = replace_playlist(plex, replace_key, push_keys, title=title, summary=summary or "")
                action, rating_key = "replaced", replace_key
            except PlexNotFound:
                # Deleted in Plex since we made it: make a fresh one and retire the old record.
                res = create_playlist(plex, title, push_keys, summary)
                action, rating_key = "recreated", res["ratingKey"]
                conn.execute("DELETE FROM managed_playlists WHERE plex_playlist_key = ?", (replace_key,))
        else:
            res = create_playlist(plex, title, push_keys, summary)
            action, rating_key = "created", res["ratingKey"]
        url = web_url(settings.get("plex_url"), plex.machine_id, rating_key)
    finally:
        if own:
            plex.close()

    with conn:
        register_managed(conn, rating_key, title, len(push_keys), draft_id=draft_id)
        conn.execute(
            "INSERT INTO pushes(draft_id, plex_playlist_key, title, action, track_count, skipped_json, pushed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (draft_id, rating_key, title, action, len(push_keys), json.dumps(skipped), _now()),
        )
        conn.execute("UPDATE drafts SET status = 'pushed', updated_at = ? WHERE id = ?", (_now(), draft_id))
    log.info("push.done", draft_id=draft_id, action=action, rating_key=rating_key, tracks=len(push_keys),
             clear_method=res.get("clear_method"))
    return {"action": action, "ratingKey": rating_key, "track_count": len(push_keys), "skipped": skipped,
            "url": url, "title": title}
