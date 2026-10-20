"""Pull Plex music sections into the local cache.

Full sync: every artist/album/track in the section; anything not seen is removed
(tracks soft-deleted via removed_at, artists/albums hard-deleted).

Incremental sync: items with updatedAt or lastViewedAt since the last sync (minus a
small overlap for clock skew). Plex doesn't report deletions, so afterwards we compare
the section's track count with ours and escalate to a full sync on mismatch.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from ...logs import get_logger
from .client import ALBUM, ARTIST, TRACK, PlexClient

log = get_logger(__name__)

OVERLAP_SEC = 300

# Tracks visible to generation and browsing: present in Plex, in an enabled section.
LIVE_TRACK_SQL = (
    "t.removed_at IS NULL AND t.section_id IN (SELECT section_id FROM libraries WHERE enabled = 1)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(epoch: Any) -> str | None:
    if epoch in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def _tags(item: dict, key: str) -> list[str]:
    return [t["tag"] for t in item.get(key) or [] if t.get("tag")]


def _jtags(item: dict, key: str) -> str:
    return json.dumps(_tags(item, key))


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
def refresh_sections(conn: sqlite3.Connection, plex: PlexClient) -> list[dict]:
    """Mirror Plex's music sections into `libraries`, keeping each one's enabled flag.
    If there's exactly one music section and nothing is enabled yet, enable it."""
    sections = plex.music_sections()
    ids = [s["section_id"] for s in sections]
    for s in sections:
        conn.execute(
            "INSERT INTO libraries(section_id, name) VALUES(?, ?) "
            "ON CONFLICT(section_id) DO UPDATE SET name = excluded.name",
            (s["section_id"], s["title"]),
        )
    if ids:
        conn.execute(
            f"DELETE FROM libraries WHERE section_id NOT IN ({','.join('?' * len(ids))})", ids
        )
    else:
        conn.execute("DELETE FROM libraries")
    if len(ids) == 1 and not conn.execute("SELECT 1 FROM libraries WHERE enabled = 1").fetchone():
        conn.execute("UPDATE libraries SET enabled = 1 WHERE section_id = ?", (ids[0],))
    conn.commit()
    return [dict(r) for r in conn.execute("SELECT * FROM libraries ORDER BY name")]


# --------------------------------------------------------------------------- #
# row mapping
# --------------------------------------------------------------------------- #
def _artist_row(a: dict, section_id: int) -> tuple:
    return (
        str(a["ratingKey"]),
        a.get("title", ""),
        _jtags(a, "Genre"),
        _jtags(a, "Style"),
        _jtags(a, "Mood"),
        (_tags(a, "Country") or [None])[0],
        section_id,
        _iso(a.get("updatedAt")),
    )


def _album_row(a: dict, section_id: int) -> tuple:
    return (
        str(a["ratingKey"]),
        str(a.get("parentRatingKey") or ""),
        a.get("title", ""),
        a.get("year"),
        _jtags(a, "Genre"),
        _jtags(a, "Style"),
        _jtags(a, "Mood"),
        section_id,
        _iso(a.get("updatedAt")),
    )


def _track_row(t: dict, section_id: int) -> tuple:
    media = (t.get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]
    return (
        str(t["ratingKey"]),
        t.get("title", ""),
        str(t.get("grandparentRatingKey") or ""),
        t.get("grandparentTitle"),
        t.get("originalTitle"),
        str(t.get("parentRatingKey") or ""),
        t.get("parentTitle"),
        t.get("index"),
        t.get("parentIndex"),
        t.get("parentYear") or t.get("year"),
        t.get("duration"),
        t.get("userRating"),
        t.get("rating"),
        t.get("viewCount", 0) or 0,
        t.get("skipCount", 0) or 0,
        _iso(t.get("lastViewedAt")),
        _iso(t.get("addedAt")),
        media.get("audioCodec"),
        media.get("bitrate"),
        part.get("file"),
        _jtags(t, "Genre"),
        _jtags(t, "Mood"),
        section_id,
        _iso(t.get("updatedAt")),
        t.get("ratingCount"),
    )


_UPSERT_ARTIST = (
    "INSERT INTO artists(plex_key,name,genres,styles,moods,country,section_id,plex_updated_at) "
    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(plex_key) DO UPDATE SET "
    "name=excluded.name, genres=excluded.genres, styles=excluded.styles, moods=excluded.moods, "
    "country=excluded.country, section_id=excluded.section_id, plex_updated_at=excluded.plex_updated_at"
)
_UPSERT_ALBUM = (
    "INSERT INTO albums(plex_key,artist_key,title,year,genres,styles,moods,section_id,plex_updated_at) "
    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(plex_key) DO UPDATE SET "
    "artist_key=excluded.artist_key, title=excluded.title, year=excluded.year, "
    "genres=excluded.genres, styles=excluded.styles, moods=excluded.moods, "
    "section_id=excluded.section_id, plex_updated_at=excluded.plex_updated_at"
)
_TRACK_COLS = (
    "plex_key,title,artist_key,artist_name,track_artist,album_key,album_title,track_num,disc_num,"
    "year,duration_ms,user_rating,critic_rating,play_count,skip_count,last_played_at,added_at,"
    "codec,bitrate,file_path,genres,moods,section_id,plex_updated_at,listeners"
)
_UPSERT_TRACK = (
    f"INSERT INTO tracks({_TRACK_COLS}) VALUES({','.join('?' * len(_TRACK_COLS.split(',')))}) "
    "ON CONFLICT(plex_key) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _TRACK_COLS.split(",")[1:])
    + ", removed_at=NULL"
)


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def _fill_temp(conn: sqlite3.Connection, name: str, keys) -> None:
    conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name}(k TEXT PRIMARY KEY)")
    conn.execute(f"DELETE FROM {name}")
    conn.executemany(f"INSERT OR IGNORE INTO {name}(k) VALUES(?)", ((k,) for k in keys))


def sync_section(
    conn: sqlite3.Connection,
    plex: PlexClient,
    section_id: int,
    *,
    full: bool = False,
    deep_tracks: bool = True,
) -> dict[str, Any]:
    t0 = time.monotonic()
    lib = conn.execute("SELECT * FROM libraries WHERE section_id = ?", (section_id,)).fetchone()
    if lib is None:
        raise ValueError(f"unknown library section {section_id}")
    started = _now_iso()

    since = None
    if not full and lib["last_sync_at"]:
        since = _epoch(lib["last_sync_at"]) - OVERLAP_SEC
    full = since is None
    changed = {"updatedAt>>": since} if since else None

    # Artists/albums: listing gives keys, a batched metadata pass gives every tag.
    artist_keys = [str(a["ratingKey"]) for a in plex.iter_items(section_id, ARTIST, changed)]
    artists = [_artist_row(a, section_id) for a in plex.metadata(artist_keys)]
    album_keys = [str(a["ratingKey"]) for a in plex.iter_items(section_id, ALBUM, changed)]
    albums = [_album_row(a, section_id) for a in plex.metadata(album_keys)]

    tracks: dict[str, dict] = {str(t["ratingKey"]): t for t in plex.iter_items(section_id, TRACK, changed)}
    if not full:
        for t in plex.iter_items(section_id, TRACK, {"lastViewedAt>>": since}):
            tracks.setdefault(str(t["ratingKey"]), t)
    if deep_tracks and tracks:
        for t in plex.metadata(list(tracks)):
            tracks[str(t["ratingKey"])] = t
    track_rows = [_track_row(t, section_id) for t in tracks.values()]

    stats: dict[str, Any] = {
        "section_id": section_id,
        "mode": "full" if full else "incremental",
        "artists": len(artists),
        "albums": len(albums),
        "tracks": len(track_rows),
        "removed": 0,
    }

    with conn:
        conn.executemany(_UPSERT_ARTIST, artists)
        conn.executemany(_UPSERT_ALBUM, albums)
        conn.executemany(_UPSERT_TRACK, track_rows)

        if full:
            now = _now_iso()
            _fill_temp(conn, "seen_tracks", tracks.keys())
            stats["removed"] = conn.execute(
                "UPDATE tracks SET removed_at = ? WHERE section_id = ? AND removed_at IS NULL "
                "AND plex_key NOT IN (SELECT k FROM seen_tracks)",
                (now, section_id),
            ).rowcount
            _fill_temp(conn, "seen_artists", artist_keys)
            conn.execute(
                "DELETE FROM artists WHERE section_id = ? AND plex_key NOT IN (SELECT k FROM seen_artists)",
                (section_id,),
            )
            _fill_temp(conn, "seen_albums", album_keys)
            conn.execute(
                "DELETE FROM albums WHERE section_id = ? AND plex_key NOT IN (SELECT k FROM seen_albums)",
                (section_id,),
            )
            _rebuild_plex_tags(conn, section_id=section_id)
        else:
            _rebuild_plex_tags(
                conn,
                section_id=section_id,
                track_keys=list(tracks),
                artist_keys=artist_keys,
                album_keys=album_keys,
            )

    if not full:
        ours = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE section_id = ? AND removed_at IS NULL", (section_id,)
        ).fetchone()[0]
        theirs = plex.count(section_id, TRACK)
        if ours != theirs:
            log.info("sync.escalate_full", section_id=section_id, ours=ours, plex=theirs)
            result = sync_section(conn, plex, section_id, full=True, deep_tracks=deep_tracks)
            result["escalated_from_incremental"] = True
            return result

    live = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE section_id = ? AND removed_at IS NULL", (section_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE libraries SET last_sync_at = ?, track_count = ?"
        + (", last_full_sync_at = ?" if full else "")
        + " WHERE section_id = ?",
        (started, live, started, section_id) if full else (started, live, section_id),
    )
    conn.commit()
    stats["live_tracks"] = live
    stats["seconds"] = round(time.monotonic() - t0, 1)
    log.info("sync.section_done", **stats)
    return stats


def sync_all(
    conn: sqlite3.Connection, plex: PlexClient, *, full: bool = False, deep_tracks: bool = True
) -> dict[str, Any]:
    refresh_sections(conn, plex)
    out = {}
    for lib in conn.execute("SELECT section_id, name FROM libraries WHERE enabled = 1").fetchall():
        out[lib["name"]] = sync_section(
            conn, plex, lib["section_id"], full=full, deep_tracks=deep_tracks
        )
    return out


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #
def _rebuild_plex_tags(
    conn: sqlite3.Connection,
    *,
    section_id: int,
    track_keys: list[str] | None = None,
    artist_keys: list[str] | None = None,
    album_keys: list[str] | None = None,
) -> None:
    """Denormalize artist + album + track genres/styles/moods onto each track as tags.
    With no keys, rebuilds the whole section; otherwise only tracks touched by a change
    to the track itself, its album, or its artist."""
    if track_keys is None:
        scope = "tr.section_id = ?"
        params: list = [section_id]
    else:
        _fill_temp(conn, "chg_tracks", track_keys)
        _fill_temp(conn, "chg_artists", artist_keys or [])
        _fill_temp(conn, "chg_albums", album_keys or [])
        scope = (
            "(tr.plex_key IN (SELECT k FROM chg_tracks) OR tr.artist_key IN (SELECT k FROM chg_artists) "
            "OR tr.album_key IN (SELECT k FROM chg_albums))"
        )
        params = []

    rows = conn.execute(
        f"""
        SELECT tr.id AS track_id, tr.genres AS t_genres, tr.moods AS t_moods,
               ar.genres AS a_genres, ar.styles AS a_styles, ar.moods AS a_moods,
               al.genres AS b_genres, al.styles AS b_styles, al.moods AS b_moods
        FROM tracks tr
        LEFT JOIN artists ar ON ar.plex_key = tr.artist_key
        LEFT JOIN albums  al ON al.plex_key = tr.album_key
        WHERE {scope}
        """,
        params,
    ).fetchall()
    if not rows:
        return

    def j(v):
        return set(json.loads(v or "[]"))

    ids = [r["track_id"] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        conn.execute(
            f"DELETE FROM track_tags WHERE source LIKE 'plex-%' AND track_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )

    insert = []
    for r in rows:
        buckets = {
            "plex-genre": j(r["a_genres"]) | j(r["b_genres"]) | j(r["t_genres"]),
            "plex-style": j(r["a_styles"]) | j(r["b_styles"]),
            "plex-mood": j(r["a_moods"]) | j(r["b_moods"]) | j(r["t_moods"]),
        }
        for source, tags in buckets.items():
            for tag in tags:
                insert.append((r["track_id"], source, tag.strip().lower(), 1.0))
    conn.executemany(
        "INSERT OR IGNORE INTO track_tags(track_id, source, tag, weight) VALUES(?,?,?,?)", insert
    )
