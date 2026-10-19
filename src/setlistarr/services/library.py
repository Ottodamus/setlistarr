"""Library operations shared by the web UI, scheduler and CLI."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import Settings
from ..core.enrich import EnrichConfig, enrich
from ..core.plex.client import client_from_settings
from ..core.plex.playlist_sync import sync_playlists
from ..core.plex.sync import LIVE_TRACK_SQL, sync_all


def do_sync(conn: sqlite3.Connection, *, full: bool) -> dict[str, Any]:
    settings = Settings(conn)
    with client_from_settings(settings) as plex:
        result = sync_all(conn, plex, full=full)
        result["_playlists"] = sync_playlists(conn, plex)
    if full and settings.get("enrich_after_full_sync"):
        result["_enrich"] = do_enrich(conn)
    return result


def do_enrich(conn: sqlite3.Connection, limit: int | None = None) -> dict[str, Any]:
    return enrich(conn, EnrichConfig.from_settings(Settings(conn)), limit=limit)


# --------------------------------------------------------------------------- #
# browsing
# --------------------------------------------------------------------------- #
TRACK_SORTS = {
    "artist": "LOWER(t.artist_name), t.year, LOWER(t.album_title), t.disc_num, t.track_num",
    "title": "LOWER(t.title)",
    "year": "t.year DESC, LOWER(t.artist_name)",
    "rating": "t.user_rating DESC NULLS LAST, t.play_count DESC",
    "plays": "t.play_count DESC",
    "added": "t.added_at DESC",
    "last_played": "t.last_played_at DESC NULLS LAST",
}


_SEP_ROW, _SEP_COL = "\x1e", "\x1f"  # record/unit separators for GROUP_CONCAT round-trips


def _playlist_list(raw: str | None) -> list[dict[str, Any]]:
    """'key US title US managed RS ...' -> [{key, title, managed}] sorted by title."""
    out = []
    for item in (raw or "").split(_SEP_ROW):
        if item:
            key, title, managed = item.split(_SEP_COL)
            out.append({"key": key, "title": title, "managed": managed == "1"})
    return sorted(out, key=lambda p: p["title"].lower())


def playlist_choices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Playlists that contain at least one live library track, for the filter dropdown."""
    return [dict(r) for r in conn.execute(
        f"""SELECT pp.rating_key AS key, pp.title, pp.smart,
                   EXISTS (SELECT 1 FROM managed_playlists m WHERE m.plex_playlist_key = pp.rating_key) AS managed,
                   (SELECT COUNT(*) FROM playlist_tracks pt JOIN tracks t ON t.plex_key = pt.plex_key
                    WHERE pt.playlist_key = pp.rating_key AND {LIVE_TRACK_SQL}) AS n
            FROM plex_playlists pp ORDER BY LOWER(pp.title)""")
        if r["n"]]


_TRACK_PLAYLISTS = f"""(SELECT GROUP_CONCAT(pp.rating_key || '{_SEP_COL}' || pp.title || '{_SEP_COL}' ||
                         EXISTS (SELECT 1 FROM managed_playlists m WHERE m.plex_playlist_key = pp.rating_key),
                         '{_SEP_ROW}')
                      FROM plex_playlists pp
                      WHERE pp.rating_key IN (SELECT playlist_key FROM playlist_tracks WHERE plex_key = t.plex_key))"""


def browse_tracks(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    tag: str = "",
    decade: int | None = None,
    sort: str = "artist",
    page: int = 1,
    per_page: int = 50,
    playlist: str = "",
) -> dict[str, Any]:
    """playlist: '' (any), 'in' (in at least one), 'none' (in no playlist), or a playlist ratingKey."""
    where, params = [LIVE_TRACK_SQL], []
    if playlist == "in":
        where.append("EXISTS (SELECT 1 FROM playlist_tracks pt WHERE pt.plex_key = t.plex_key)")
    elif playlist == "none":
        where.append("NOT EXISTS (SELECT 1 FROM playlist_tracks pt WHERE pt.plex_key = t.plex_key)")
    elif playlist:
        where.append("EXISTS (SELECT 1 FROM playlist_tracks pt WHERE pt.plex_key = t.plex_key AND pt.playlist_key = ?)")
        params.append(playlist)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        where.append(
            "(LOWER(t.title) LIKE ? OR LOWER(t.artist_name) LIKE ? OR LOWER(t.album_title) LIKE ? "
            "OR LOWER(COALESCE(t.track_artist, '')) LIKE ?)"
        )
        params += [like] * 4
    if tag.strip():
        where.append("EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_id = t.id AND tt.tag = ?)")
        params.append(tag.strip().lower())
    if decade:
        where.append("t.year BETWEEN ? AND ?")
        params += [decade, decade + 9]
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM tracks t WHERE {clause}", params).fetchone()[0]
    page = max(1, page)
    rows = conn.execute(
        f"""
        SELECT t.plex_key, t.title, t.artist_name, t.track_artist, t.album_title, t.year,
               t.duration_ms, t.user_rating, t.play_count, t.last_played_at,
               (SELECT GROUP_CONCAT(tag, ', ') FROM
                   (SELECT DISTINCT tag FROM track_tags tt WHERE tt.track_id = t.id
                    AND tt.source LIKE 'plex-%' LIMIT 4)) AS tags,
               {_TRACK_PLAYLISTS} AS playlists_raw
        FROM tracks t WHERE {clause}
        ORDER BY {TRACK_SORTS.get(sort, TRACK_SORTS['artist'])}
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["playlists"] = _playlist_list(d.pop("playlists_raw"))
        out.append(d)
    return {
        "rows": out,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // per_page)),
    }


def browse_artists(conn: sqlite3.Connection, *, q: str = "", page: int = 1, per_page: int = 60) -> dict:
    where, params = [LIVE_TRACK_SQL], []
    if q.strip():
        where.append("LOWER(t.artist_name) LIKE ?")
        params.append(f"%{q.strip().lower()}%")
    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(DISTINCT t.artist_key) FROM tracks t WHERE {clause}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT t.artist_name AS name, COUNT(*) AS tracks, COUNT(DISTINCT t.album_key) AS albums,
               MIN(t.year) AS first_year, MAX(t.year) AS last_year, SUM(t.play_count) AS plays,
               ar.genres AS genres,
               (SELECT GROUP_CONCAT(pp.rating_key || '{_SEP_COL}' || pp.title || '{_SEP_COL}' ||
                         EXISTS (SELECT 1 FROM managed_playlists m WHERE m.plex_playlist_key = pp.rating_key),
                         '{_SEP_ROW}')
                FROM plex_playlists pp WHERE pp.rating_key IN (
                    SELECT pt.playlist_key FROM playlist_tracks pt JOIN tracks t2 ON t2.plex_key = pt.plex_key
                    WHERE t2.artist_key = t.artist_key)) AS playlists_raw
        FROM tracks t LEFT JOIN artists ar ON ar.plex_key = t.artist_key
        WHERE {clause}
        GROUP BY t.artist_key ORDER BY LOWER(t.artist_name)
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (max(1, page) - 1) * per_page],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["playlists"] = _playlist_list(d.pop("playlists_raw"))
        out.append(d)
    return {"rows": out, "total": total, "page": max(1, page), "pages": max(1, -(-total // per_page))}


def library_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    live = LIVE_TRACK_SQL
    totals = dict(
        conn.execute(
            f"""
            SELECT COUNT(*) AS tracks,
                   COUNT(DISTINCT t.artist_key) AS artists,
                   COUNT(DISTINCT t.album_key) AS albums,
                   COALESCE(SUM(t.duration_ms), 0) AS duration_ms,
                   SUM(CASE WHEN t.play_count = 0 THEN 1 ELSE 0 END) AS never_played,
                   SUM(CASE WHEN t.user_rating >= 8 THEN 1 ELSE 0 END) AS rated_4plus,
                   SUM(CASE WHEN t.user_rating IS NOT NULL THEN 1 ELSE 0 END) AS rated
            FROM tracks t WHERE {live}
            """
        ).fetchone()
    )
    enriched = conn.execute(
        f"""SELECT COUNT(DISTINCT tt.track_id) FROM track_tags tt JOIN tracks t ON t.id = tt.track_id
            WHERE tt.source LIKE 'lastfm-%' AND {live}"""
    ).fetchone()[0]
    totals["enriched"] = enriched

    def top(source_like: str, n: int = 20):
        return [
            dict(r)
            for r in conn.execute(
                f"""SELECT tt.tag, COUNT(DISTINCT tt.track_id) AS n FROM track_tags tt
                    JOIN tracks t ON t.id = tt.track_id
                    WHERE tt.source LIKE ? AND {live} GROUP BY tt.tag ORDER BY n DESC LIMIT ?""",
                (source_like, n),
            )
        ]

    decades = [
        dict(r)
        for r in conn.execute(
            f"""SELECT (t.year/10)*10 AS decade, COUNT(*) AS n FROM tracks t
                WHERE t.year IS NOT NULL AND {live} GROUP BY decade ORDER BY decade"""
        )
    ]
    top_artists = [
        dict(r)
        for r in conn.execute(
            f"""SELECT t.artist_name AS name, COUNT(*) AS n, SUM(t.play_count) AS plays
                FROM tracks t WHERE {live} GROUP BY t.artist_key ORDER BY plays DESC LIMIT 15"""
        )
    ]
    most_played = [
        dict(r)
        for r in conn.execute(
            f"""SELECT t.title, COALESCE(t.track_artist, t.artist_name) AS artist, t.play_count
                FROM tracks t WHERE {live} AND t.play_count > 0 ORDER BY t.play_count DESC LIMIT 15"""
        )
    ]
    libraries = [dict(r) for r in conn.execute("SELECT * FROM libraries ORDER BY name")]
    return {
        "totals": totals,
        "genres": top("plex-genre"),
        "moods": top("plex-mood"),
        "styles": top("plex-style"),
        "lastfm": top("lastfm-%"),
        "decades": decades,
        "top_artists": top_artists,
        "most_played": most_played,
        "libraries": libraries,
    }
