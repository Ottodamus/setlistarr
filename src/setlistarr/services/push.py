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
from ..core.plex.playlists import create_playlist, replace_playlist, upload_poster, web_url
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
        cover_status, cover_error = _push_cover(conn, plex, draft_id, rating_key, fresh=action != "replaced")
    finally:
        if own:
            plex.close()

    with conn:
        register_managed(conn, rating_key, title, len(push_keys), draft_id=draft_id)
        if cover_status == "uploaded":
            conn.execute("UPDATE managed_playlists SET cover_id = (SELECT cover_id FROM drafts WHERE id = ?) "
                         "WHERE plex_playlist_key = ?", (draft_id, rating_key))
        conn.execute(
            "INSERT INTO pushes(draft_id, plex_playlist_key, title, action, track_count, skipped_json, pushed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (draft_id, rating_key, title, action, len(push_keys), json.dumps(skipped), _now()),
        )
        conn.execute("UPDATE drafts SET status = 'pushed', updated_at = ? WHERE id = ?", (_now(), draft_id))
    from ..core.plex.playlist_sync import set_members

    set_members(conn, rating_key, push_keys, title=title)  # library view is current without waiting for a sync
    if action == "recreated" and replace_key:
        conn.execute("DELETE FROM plex_playlists WHERE rating_key = ?", (replace_key,))
        conn.commit()
    log.info("push.done", draft_id=draft_id, action=action, rating_key=rating_key, tracks=len(push_keys),
             clear_method=res.get("clear_method"))
    return {"action": action, "ratingKey": rating_key, "track_count": len(push_keys), "skipped": skipped,
            "url": url, "title": title, "cover": cover_status, "cover_error": cover_error}


def _push_cover(conn, plex: PlexClient, draft_id: int, rating_key: str, *, fresh: bool) -> tuple[str, str | None]:
    """Upload the draft's cover as the playlist poster. Returns (status, error) where status is
    'none' (draft has no cover - Plex's is left alone), 'unchanged', 'uploaded' or 'failed'.
    A failed cover upload never fails the push."""
    from .covers import cover_bytes

    row = conn.execute("SELECT cover_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    cover_id = row["cover_id"] if row else None
    if not cover_id:
        return "none", None
    current = conn.execute("SELECT cover_id FROM managed_playlists WHERE plex_playlist_key = ?",
                           (rating_key,)).fetchone()
    if not fresh and current and current["cover_id"] == cover_id:
        return "unchanged", None
    data = cover_bytes(conn, cover_id)
    if data is None:
        return "failed", "the cover file is missing from /config/covers"
    try:
        upload_poster(plex, rating_key, data)
    except PlexError as e:
        log.warning("push.cover_failed", rating_key=rating_key, error=str(e))
        return "failed", str(e)
    return "uploaded", None
