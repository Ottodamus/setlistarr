"""The in-progress draft. This is the grounding gate: a track only gets in if its plex_key is
a live library track and it passes every constraint. Rejections carry a reason the model
(or the UI) can act on."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Any

from ..core.plex.sync import LIVE_TRACK_SQL
from .constraints import Constraints

_PAREN = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_SUFFIX = re.compile(r"\s+-\s+.*$")
_FEAT = re.compile(r"\s+(feat\.?|featuring|ft\.?)\s+.*$", re.I)
_NONWORD = re.compile(r"[^\w]+")


def norm_title(title: str) -> str:
    """'Song (2011 Remaster)' / 'Song - Live at X' / 'Song [Mono]' -> 'song'."""
    t = _PAREN.sub(" ", title or "")
    t = _SUFFIX.sub("", t)
    return _NONWORD.sub("", t.lower())


def norm_artist(artist: str) -> str:
    """Primary artist for per-artist caps: 'A feat. B' -> 'a'."""
    a = _FEAT.sub("", (artist or "").strip())
    return _NONWORD.sub("", a.lower()) or _NONWORD.sub("", (artist or "").lower())


def fetch_tracks(conn: sqlite3.Connection, keys: list[str], *, with_tags: bool = False) -> dict[str, dict]:
    """Live library rows for the given keys (unknown/removed keys are simply absent)."""
    out: dict[str, dict] = {}
    keys = [str(k) for k in keys]
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        rows = conn.execute(
            f"""SELECT t.id, t.plex_key, t.title, COALESCE(t.track_artist, t.artist_name) AS artist,
                       t.artist_name AS album_artist, t.album_title AS album, t.year, t.duration_ms
                FROM tracks t WHERE t.plex_key IN ({','.join('?' * len(chunk))}) AND {LIVE_TRACK_SQL}""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["plex_key"]] = dict(r)
    if with_tags and out:
        ids = {r["id"]: k for k, r in out.items()}
        for k in out:
            out[k]["tags"] = set()
        id_list = list(ids)
        for i in range(0, len(id_list), 500):
            chunk = id_list[i : i + 500]
            for r in conn.execute(
                f"SELECT track_id, tag FROM track_tags WHERE track_id IN ({','.join('?' * len(chunk))})", chunk
            ):
                out[ids[r["track_id"]]]["tags"].add(r["tag"])
    return out


class DraftState:
    def __init__(self, conn: sqlite3.Connection, constraints: Constraints, *, enforce_duration: bool = True):
        self.conn = conn
        self.c = constraints
        self.enforce_duration = enforce_duration
        self.tracks: list[dict[str, Any]] = []
        self.pool: dict[str, None] = {}  # ordered set of every key a tool showed the model
        self.locked: set[str] = set()  # user-kept tracks: never rejected on constraints, never removed
        self._excluded_keys = {str(k) for k in constraints.exclude_keys}

    # ------------------------------------------------------------------ #
    @property
    def keys(self) -> list[str]:
        return [t["plex_key"] for t in self.tracks]

    @property
    def total_ms(self) -> int:
        return sum(t["duration_ms"] or 0 for t in self.tracks)

    def artist_counts(self) -> Counter:
        return Counter(norm_artist(t["artist"]) for t in self.tracks)

    def remember(self, keys) -> None:
        for k in keys:
            self.pool.setdefault(str(k), None)

    # ------------------------------------------------------------------ #
    def _reject_reason(self, row: dict | None, key: str, force: bool = False) -> str | None:
        if row is None:
            return "not a track in this library (unknown or removed plex_key)"
        if key in self.keys:
            return "already in the draft"
        if force:
            return None
        song = (norm_artist(row["artist"]), norm_title(row["title"]))
        if any((norm_artist(t["artist"]), norm_title(t["title"])) == song for t in self.tracks):
            return "same song is already in the draft (another version/album)"
        if key in self._excluded_keys:
            return "was in the previous refresh of this playlist"
        excluded = {norm_artist(a) for a in self.c.exclude_artists}
        if norm_artist(row["artist"]) in excluded or norm_artist(row["album_artist"] or "") in excluded:
            return "artist is excluded"
        if self.c.exclude_tags and row.get("tags") and row["tags"] & set(self.c.exclude_tags):
            return "has an excluded tag: " + ", ".join(sorted(row["tags"] & set(self.c.exclude_tags)))
        if self.c.year_min and row["year"] and row["year"] < self.c.year_min:
            return f"year {row['year']} is before {self.c.year_min}"
        if self.c.year_max and row["year"] and row["year"] > self.c.year_max:
            return f"year {row['year']} is after {self.c.year_max}"
        if self.artist_counts()[norm_artist(row["artist"])] >= self.c.per_artist:
            return f"artist already has {self.c.per_artist} tracks (max per artist)"
        if self.enforce_duration:
            _, hi = self.c.window_ms
            if self.total_ms + (row["duration_ms"] or 0) > hi:
                return f"would exceed the target length (~{self.c.target:g} min)"
        return None

    def add(self, keys: list[str], *, position: int | None = None, source: str = "model",
            force: bool = False) -> dict[str, Any]:
        """force=True (user-kept tracks) only checks the track exists and isn't a duplicate."""
        keys = [str(k) for k in keys]
        rows = fetch_tracks(self.conn, keys, with_tags=bool(self.c.exclude_tags))
        accepted, rejected = [], []
        insert_at = len(self.tracks) if position is None else max(0, min(int(position), len(self.tracks)))
        for k in keys:
            row = rows.get(k)
            reason = self._reject_reason(row, k, force)
            if reason:
                rejected.append({"plex_key": k, "reason": reason})
                continue
            row = dict(row, source=source)
            row.pop("tags", None)
            self.tracks.insert(insert_at, row)
            insert_at += 1
            accepted.append(k)
        return {"accepted": accepted, "rejected": rejected, **self.totals()}

    def remove(self, keys: list[str]) -> dict[str, Any]:
        drop = {str(k) for k in keys}
        kept = sorted(drop & self.locked)
        drop -= self.locked
        before = len(self.tracks)
        self.tracks = [t for t in self.tracks if t["plex_key"] not in drop]
        res = {"removed": before - len(self.tracks), **self.totals()}
        if kept:
            res["not_removed"] = {"plex_keys": kept, "reason": "kept by the user"}
        return res

    def reorder(self, keys: list[str]) -> dict[str, Any]:
        keys = [str(k) for k in keys]
        if sorted(keys) != sorted(self.keys):
            return {"error": "plex_keys must be exactly the current draft's keys, in the new order",
                    "current": self.keys}
        by_key = {t["plex_key"]: t for t in self.tracks}
        self.tracks = [by_key[k] for k in keys]
        return {"ok": True, **self.totals()}

    def totals(self) -> dict[str, Any]:
        lo, hi = self.c.window_ms
        return {
            "track_count": len(self.tracks),
            "minutes": round(self.total_ms / 60000, 1),
            "target_minutes": f"{lo / 60000:.0f}-{hi / 60000:.0f}",
            "remaining_minutes": round(max(0, lo - self.total_ms) / 60000, 1),
        }

    def view(self) -> dict[str, Any]:
        return {
            "constraints": self.c.describe(),
            **self.totals(),
            "tracks": [
                {"plex_key": t["plex_key"], "artist": t["artist"], "title": t["title"], "year": t["year"],
                 "duration_sec": round((t["duration_ms"] or 0) / 1000)}
                for t in self.tracks
            ],
            "artists_at_cap": sorted(
                t["artist"] for t in {norm_artist(t["artist"]): t for t in self.tracks}.values()
                if self.artist_counts()[norm_artist(t["artist"])] >= self.c.per_artist
            ),
        }
