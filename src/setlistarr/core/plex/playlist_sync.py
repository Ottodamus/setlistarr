"""Mirror the Plex user's audio playlists (membership only) into plex_playlists/playlist_tracks.

Read-only against Plex. Playlists whose updatedAt hasn't changed since the last sync aren't
re-read, so this is cheap on every incremental sync.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .client import PlexClient
from .playlists import list_audio_playlists, playlist_items


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_playlists(conn: sqlite3.Connection, plex: PlexClient) -> dict[str, Any]:
    remote = list_audio_playlists(plex)
    known = {r["rating_key"]: r["updated_at"] for r in conn.execute("SELECT rating_key, updated_at FROM plex_playlists")}
    seen, reread = set(), 0
    for p in remote:
        key = str(p["ratingKey"])
        seen.add(key)
        updated = str(p.get("updatedAt") or "")
        with conn:
            conn.execute(
                "INSERT INTO plex_playlists(rating_key, title, smart, track_count, updated_at, synced_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(rating_key) DO UPDATE SET title=excluded.title, "
                "smart=excluded.smart, track_count=excluded.track_count, synced_at=excluded.synced_at",
                (key, p.get("title") or "", int(bool(p.get("smart"))), p.get("leafCount"), known.get(key), _now()),
            )
        if key in known and known[key] == updated and updated:
            continue
        set_members(conn, key, [str(i["ratingKey"]) for i in playlist_items(plex, key) if i.get("ratingKey")],
                    updated_at=updated)
        reread += 1
    gone = [k for k in known if k not in seen]
    with conn:
        for k in gone:
            conn.execute("DELETE FROM plex_playlists WHERE rating_key = ?", (k,))
    return {"playlists": len(remote), "reread": reread, "removed": len(gone)}


def set_members(conn: sqlite3.Connection, playlist_key: str, track_keys: list[str], *,
                title: str | None = None, updated_at: str | None = None) -> None:
    """Replace one playlist's membership (also used right after a push)."""
    with conn:
        if title is not None:
            conn.execute(
                "INSERT INTO plex_playlists(rating_key, title, track_count, synced_at) VALUES(?,?,?,?) "
                "ON CONFLICT(rating_key) DO UPDATE SET title=excluded.title, track_count=excluded.track_count",
                (playlist_key, title, len(track_keys), _now()),
            )
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_key = ?", (playlist_key,))
        conn.executemany("INSERT INTO playlist_tracks(playlist_key, position, plex_key) VALUES(?,?,?)",
                         [(playlist_key, i, k) for i, k in enumerate(track_keys)])
        conn.execute("UPDATE plex_playlists SET track_count = ?, updated_at = COALESCE(?, updated_at) "
                     "WHERE rating_key = ?", (len(track_keys), updated_at, playlist_key))
