"""Draft editing. User edits are intentional, so they aren't blocked by constraints (only by
"must be a real library track" and "no exact duplicate"); constraint breaks surface as warnings."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..generation.constraints import Constraints
from ..generation.draft import fetch_tracks, norm_artist, norm_title
from .generations import load_draft


class DraftError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(conn: sqlite3.Connection, draft_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT plex_key, source, locked FROM draft_tracks WHERE draft_id = ? ORDER BY position", (draft_id,)
    )]


def _write(conn: sqlite3.Connection, draft_id: int, rows: list[dict[str, Any]]) -> None:
    with conn:
        conn.execute("DELETE FROM draft_tracks WHERE draft_id = ?", (draft_id,))
        conn.executemany(
            "INSERT INTO draft_tracks(draft_id, position, plex_key, source, locked) VALUES(?,?,?,?,?)",
            [(draft_id, i, r["plex_key"], r["source"], int(r["locked"])) for i, r in enumerate(rows)],
        )
        conn.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (_now(), draft_id))


def _require(conn, draft_id: int) -> None:
    if not conn.execute("SELECT 1 FROM drafts WHERE id = ?", (draft_id,)).fetchone():
        raise DraftError("No such draft")


def remove_track(conn: sqlite3.Connection, draft_id: int, plex_key: str) -> None:
    _require(conn, draft_id)
    _write(conn, draft_id, [r for r in _rows(conn, draft_id) if r["plex_key"] != str(plex_key)])


def reorder(conn: sqlite3.Connection, draft_id: int, keys: list[str]) -> None:
    _require(conn, draft_id)
    rows = _rows(conn, draft_id)
    by_key = {r["plex_key"]: r for r in rows}
    keys = [str(k) for k in keys]
    if sorted(keys) != sorted(by_key):
        raise DraftError("The draft changed in another tab - reload and try again")
    _write(conn, draft_id, [by_key[k] for k in keys])


def add_track(conn: sqlite3.Connection, draft_id: int, plex_key: str, position: int | None = None) -> None:
    _require(conn, draft_id)
    key = str(plex_key)
    if not fetch_tracks(conn, [key]):
        raise DraftError("That track isn't in the library any more")
    rows = _rows(conn, draft_id)
    if any(r["plex_key"] == key for r in rows):
        raise DraftError("That track is already in the draft")
    row = {"plex_key": key, "source": "user", "locked": 1}  # hand-picked tracks start locked
    rows.insert(len(rows) if position is None else max(0, min(position, len(rows))), row)
    _write(conn, draft_id, rows)


def toggle_lock(conn: sqlite3.Connection, draft_id: int, plex_key: str) -> None:
    _require(conn, draft_id)
    rows = _rows(conn, draft_id)
    for r in rows:
        if r["plex_key"] == str(plex_key):
            r["locked"] = 0 if r["locked"] else 1
    _write(conn, draft_id, rows)


def update_meta(conn: sqlite3.Connection, draft_id: int, title: str, description: str) -> None:
    _require(conn, draft_id)
    title = title.strip()
    if not title:
        raise DraftError("Title can't be empty")
    with conn:
        conn.execute("UPDATE drafts SET title = ?, description = ?, updated_at = ? WHERE id = ?",
                     (title, description.strip() or None, _now(), draft_id))


def draft_constraints(conn: sqlite3.Connection, draft_id: int) -> Constraints:
    row = conn.execute(
        "SELECT g.constraints_json FROM drafts d LEFT JOIN generations g ON g.id = d.generation_id WHERE d.id = ?",
        (draft_id,),
    ).fetchone()
    return Constraints.from_dict(json.loads(row["constraints_json"] or "{}")) if row else Constraints()


def warnings(conn: sqlite3.Connection, draft: dict[str, Any]) -> list[str]:
    """Constraint breaks after user edits - shown, not enforced."""
    c = draft_constraints(conn, draft["id"])
    out = []
    missing = [t for t in draft["tracks"] if t["missing"]]
    if missing:
        out.append(f"{len(missing)} track(s) are gone from Plex and will be skipped on push")
    live = [t for t in draft["tracks"] if not t["missing"]]
    over = [a for a, n in Counter(norm_artist(t["artist"] or "") for t in live).items() if n > c.per_artist]
    if over:
        names = {norm_artist(t["artist"] or ""): t["artist"] for t in live}
        out.append(f"More than {c.per_artist} tracks by: " + ", ".join(names[a] for a in over))
    songs = Counter((norm_artist(t["artist"] or ""), norm_title(t["title"] or "")) for t in live)
    dupes = [s for s, n in songs.items() if n > 1]
    if dupes:
        out.append(f"{len(dupes)} song(s) appear more than once (different versions)")
    lo, hi = c.window_ms
    total = sum(t["duration_ms"] or 0 for t in live)
    if total < lo or total > hi:
        out.append(f"Runtime {total / 60000:.0f} min is outside the ~{c.target:g} min target")
    return out


def search_library(conn: sqlite3.Connection, draft_id: int, q: str, limit: int = 15) -> list[dict[str, Any]]:
    from .library import browse_tracks

    in_draft = {r["plex_key"] for r in _rows(conn, draft_id)}
    rows = browse_tracks(conn, q=q, sort="plays", per_page=limit + len(in_draft))["rows"] if q.strip() else []
    return [r for r in rows if r["plex_key"] not in in_draft][:limit]


def load_for_editor(conn: sqlite3.Connection, draft_id: int) -> dict[str, Any] | None:
    d = load_draft(conn, draft_id)
    if not d:
        return None
    locked = {r["plex_key"]: r["locked"] for r in _rows(conn, draft_id)}
    for t in d["tracks"]:
        t["locked"] = bool(locked.get(t["plex_key"]))
    d["warnings"] = warnings(conn, d)
    d["constraints"] = draft_constraints(conn, draft_id)
    d["generation"] = dict(conn.execute("SELECT * FROM generations WHERE id = ?", (d["generation_id"],)).fetchone() or {}) \
        if d["generation_id"] else {}
    d["pushes"] = [dict(r) for r in conn.execute(
        "SELECT * FROM pushes WHERE draft_id = ? ORDER BY id DESC", (draft_id,))]
    return d
