"""Read side of generations/drafts for the UI and CLI."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any


def get_generation(conn: sqlite3.Connection, gen_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    if not row:
        return None
    g = dict(row)
    g["constraints"] = json.loads(g.pop("constraints_json") or "{}")
    g["trace"] = json.loads(g.pop("trace_json") or "[]")
    d = conn.execute("SELECT id FROM drafts WHERE generation_id = ? ORDER BY id DESC LIMIT 1", (gen_id,)).fetchone()
    g["draft_id"] = d["id"] if d else None
    return g


def recent_generations(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT g.id, g.created_at, g.prompt, g.status, g.title, g.provider_name, g.model, g.mode_used,
                      g.cost_usd, g.duration_ms, d.id AS draft_id, d.title AS playlist_title, d.cover_id,
                      (SELECT COUNT(*) FROM draft_tracks dt WHERE dt.draft_id = d.id) AS tracks,
                      (SELECT p.title FROM pushes p WHERE p.draft_id = d.id ORDER BY p.id DESC LIMIT 1) AS pushed_as
               FROM generations g
               LEFT JOIN drafts d ON d.id = (SELECT MAX(id) FROM drafts WHERE generation_id = g.id)
               ORDER BY g.id DESC LIMIT ?""",
            (limit,),
        )
    ]


def load_draft(conn: sqlite3.Connection, draft_id: int) -> dict[str, Any] | None:
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if not d:
        return None
    rows = conn.execute(
        """SELECT dt.position, dt.plex_key, dt.source, t.title, COALESCE(t.track_artist, t.artist_name) AS artist,
                  t.album_title AS album, t.year, t.duration_ms, t.user_rating, t.play_count,
                  t.removed_at IS NOT NULL OR t.id IS NULL AS missing
           FROM draft_tracks dt LEFT JOIN tracks t ON t.plex_key = dt.plex_key
           WHERE dt.draft_id = ? ORDER BY dt.position""",
        (draft_id,),
    ).fetchall()
    tracks = [dict(r) for r in rows]
    artists = Counter(t["artist"] for t in tracks if t["artist"])
    return {
        **dict(d),
        "adjustments": json.loads(d["adjustments_json"] or "[]"),
        "tracks": tracks,
        "total_ms": sum(t["duration_ms"] or 0 for t in tracks),
        "artist_count": len(artists),
        "top_artists": artists.most_common(5),
    }
