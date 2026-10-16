"""The tool layer the LLM calls. Shared by the MCP server, the CLI and (phase 2) the
web generation agent.

Design notes:
  * Everything the model filters on is a *tag* (Plex genre/style/mood + Last.fm +
    MusicBrainz), so it only ever asks for vocabulary that exists in YOUR library.
  * search_tracks never returns the whole library - it returns compact candidate
    rows the model can curate.
  * Only live tracks are visible: present in Plex (removed_at IS NULL) and in an
    enabled library section (see LIVE_TRACK_SQL).
  * Only create_plex_playlist has side effects; it is gated by allow_writes and is
    never offered to the web generation agent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .plex.client import PlexClient
from .plex.playlists import create_playlist
from .plex.sync import LIVE_TRACK_SQL

_LIVE_TAGS = f"track_id IN (SELECT t.id FROM tracks t WHERE {LIVE_TRACK_SQL})"
_OWNED_ARTIST = (
    "SELECT 1 FROM artists WHERE LOWER(name) = ? "
    "AND section_id IN (SELECT section_id FROM libraries WHERE enabled = 1)"
)


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    plex_factory: Callable[[], PlexClient] | None = None
    allow_writes: bool = False


# --------------------------------------------------------------------------- #
# list_facets
# --------------------------------------------------------------------------- #
def list_facets(ctx: ToolContext, top: int = 60) -> dict:
    conn = ctx.conn
    tag_rows = conn.execute(
        f"""
        SELECT tag, COUNT(DISTINCT track_id) AS n
        FROM track_tags WHERE {_LIVE_TAGS} GROUP BY tag ORDER BY n DESC LIMIT ?
        """,
        (top,),
    ).fetchall()

    decade_rows = conn.execute(
        f"""
        SELECT (year/10)*10 AS decade, COUNT(*) AS n
        FROM tracks t WHERE year IS NOT NULL AND {LIVE_TRACK_SQL} GROUP BY decade ORDER BY decade
        """
    ).fetchall()

    totals = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM tracks t WHERE {LIVE_TRACK_SQL}) AS tracks,
          (SELECT COUNT(DISTINCT t.artist_key) FROM tracks t WHERE {LIVE_TRACK_SQL}) AS artists,
          (SELECT COUNT(DISTINCT t.album_key) FROM tracks t WHERE {LIVE_TRACK_SQL})  AS albums,
          (SELECT COUNT(*) FROM tracks t WHERE user_rating >= 8 AND {LIVE_TRACK_SQL}) AS rated_4plus,
          (SELECT COUNT(*) FROM tracks t WHERE play_count = 0 AND {LIVE_TRACK_SQL})   AS never_played
        """
    ).fetchone()

    return {
        "totals": dict(totals),
        "top_tags": [{"tag": r["tag"], "tracks": r["n"]} for r in tag_rows],
        "decades": [{"decade": r["decade"], "tracks": r["n"]} for r in decade_rows],
        "hint": "Filter with any_tags/all_tags/exclude_tags using values from top_tags. "
        "Call search_tags to discover tags not shown here.",
    }


def search_tags(ctx: ToolContext, query: str, limit: int = 25) -> dict:
    rows = ctx.conn.execute(
        f"""
        SELECT tag, COUNT(DISTINCT track_id) AS n
        FROM track_tags WHERE tag LIKE ? AND {_LIVE_TAGS} GROUP BY tag ORDER BY n DESC LIMIT ?
        """,
        (f"%{query.strip().lower()}%", limit),
    ).fetchall()
    return {"matches": [{"tag": r["tag"], "tracks": r["n"]} for r in rows]}


# --------------------------------------------------------------------------- #
# search_tracks
# --------------------------------------------------------------------------- #
_SORTS = {
    "relevance": "tag_hits DESC, user_rating DESC",
    "rating": "user_rating DESC NULLS LAST, play_count DESC",
    "play_count": "play_count DESC",
    "least_played": "play_count ASC, user_rating DESC",
    "recently_added": "added_at DESC",
    "year": "year DESC",
    "random": "RANDOM()",
}


def search_tracks(
    ctx: ToolContext,
    any_tags: list[str] | None = None,
    all_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    artists: list[str] | None = None,
    exclude_artists: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_user_rating: float | None = None,
    min_play_count: int | None = None,
    max_play_count: int | None = None,
    never_played: bool = False,
    duration_min_sec: int | None = None,
    duration_max_sec: int | None = None,
    sort: str = "relevance",
    limit: int = 50,
    boost_tags: list[str] | None = None,
    exclude_keys: list[str] | None = None,
) -> dict:
    """any_tags filters (match at least one); boost_tags only raise relevance (e.g. moods)."""
    where: list[str] = [LIVE_TRACK_SQL]
    if exclude_keys:
        keys = [str(k) for k in exclude_keys]
        where.append(f"t.plex_key NOT IN ({','.join('?' * len(keys))})")
        params_pre = keys
    else:
        params_pre = []
    params: list = list(params_pre)

    def norm(xs):
        return [x.strip().lower() for x in xs if x and x.strip()]

    any_tags = norm(any_tags or [])
    all_tags = norm(all_tags or [])
    exclude_tags = norm(exclude_tags or [])

    if any_tags:
        placeholders = ",".join("?" * len(any_tags))
        where.append(
            f"EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_id = t.id "
            f"AND tt.tag IN ({placeholders}))"
        )
        params += any_tags
    for tag in all_tags:
        where.append("EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_id = t.id AND tt.tag = ?)")
        params.append(tag)
    for tag in exclude_tags:
        where.append(
            "NOT EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_id = t.id AND tt.tag = ?)"
        )
        params.append(tag)

    # Match the album artist or the per-track artist (compilations, features).
    if artists:
        names = [a.strip().lower() for a in artists]
        placeholders = ",".join("?" * len(names))
        where.append(
            f"(LOWER(t.artist_name) IN ({placeholders}) OR LOWER(COALESCE(t.track_artist, '')) IN ({placeholders}))"
        )
        params += names + names
    if exclude_artists:
        names = [a.strip().lower() for a in exclude_artists]
        placeholders = ",".join("?" * len(names))
        where.append(
            f"LOWER(t.artist_name) NOT IN ({placeholders}) AND LOWER(COALESCE(t.track_artist, '')) NOT IN ({placeholders})"
        )
        params += names + names

    if year_min is not None:
        where.append("t.year >= ?")
        params.append(year_min)
    if year_max is not None:
        where.append("t.year <= ?")
        params.append(year_max)
    if min_user_rating is not None:
        where.append("t.user_rating >= ?")
        params.append(min_user_rating)
    if min_play_count is not None:
        where.append("t.play_count >= ?")
        params.append(min_play_count)
    if max_play_count is not None:
        where.append("t.play_count <= ?")
        params.append(max_play_count)
    if never_played:
        where.append("t.play_count = 0")
    if duration_min_sec is not None:
        where.append("t.duration_ms >= ?")
        params.append(duration_min_sec * 1000)
    if duration_max_sec is not None:
        where.append("t.duration_ms <= ?")
        params.append(duration_max_sec * 1000)

    order = _SORTS.get(sort, _SORTS["relevance"])
    limit = max(1, min(int(limit), 200))

    hit_tags = list(dict.fromkeys(any_tags + norm(boost_tags or [])))
    tag_hit_expr = "0"
    if hit_tags:
        placeholders = ",".join("?" * len(hit_tags))
        tag_hit_expr = (
            f"(SELECT COUNT(DISTINCT tt.tag) FROM track_tags tt WHERE tt.track_id = t.id "
            f"AND tt.tag IN ({placeholders}))"
        )

    sql = f"""
        SELECT t.plex_key, t.title, COALESCE(t.track_artist, t.artist_name) AS artist_name,
               t.album_title, t.year,
               t.duration_ms, t.user_rating, t.play_count,
               {tag_hit_expr} AS tag_hits
        FROM tracks t
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT ?
    """
    query_params = hit_tags + params + [limit]
    rows = ctx.conn.execute(sql, query_params).fetchall()

    out = []
    for r in rows:
        out.append(
            {
                "plex_key": r["plex_key"],
                "title": r["title"],
                "artist": r["artist_name"],
                "album": r["album_title"],
                "year": r["year"],
                "duration_sec": round((r["duration_ms"] or 0) / 1000),
                "user_rating": r["user_rating"],
                "play_count": r["play_count"],
            }
        )
    return {"count": len(out), "tracks": out}


# --------------------------------------------------------------------------- #
# list_genres / list_artists / get_artist_tracks / sample_tracks
# --------------------------------------------------------------------------- #
_TAG_SOURCES = {"plex": "plex-%", "lastfm": "lastfm-%", "musicbrainz": "musicbrainz"}


def list_genres(ctx: ToolContext, source: str = "any", top: int = 80) -> dict:
    like = _TAG_SOURCES.get(source)
    rows = ctx.conn.execute(
        f"""
        SELECT tag, COUNT(DISTINCT track_id) AS n FROM track_tags
        WHERE {_LIVE_TAGS} {"AND source LIKE ?" if like else ""}
        GROUP BY tag ORDER BY n DESC LIMIT ?
        """,
        ([like] if like else []) + [max(1, min(int(top), 300))],
    ).fetchall()
    return {"source": source, "tags": [{"tag": r["tag"], "tracks": r["n"]} for r in rows]}


def list_artists(
    ctx: ToolContext,
    tags: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict:
    where, params = [LIVE_TRACK_SQL], []
    tags = [t.strip().lower() for t in tags or [] if t and t.strip()]
    if tags:
        where.append(
            f"EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_id = t.id AND tt.tag IN ({','.join('?' * len(tags))}))"
        )
        params += tags
    if year_min is not None:
        where.append("t.year >= ?")
        params.append(year_min)
    if year_max is not None:
        where.append("t.year <= ?")
        params.append(year_max)
    if query and query.strip():
        where.append("LOWER(t.artist_name) LIKE ?")
        params.append(f"%{query.strip().lower()}%")
    rows = ctx.conn.execute(
        f"""
        SELECT t.artist_name AS name, COUNT(*) AS tracks, MIN(t.year) AS first_year, MAX(t.year) AS last_year,
               SUM(t.play_count) AS plays
        FROM tracks t WHERE {' AND '.join(where)}
        GROUP BY t.artist_key ORDER BY tracks DESC, plays DESC LIMIT ?
        """,
        params + [max(1, min(int(limit), 200))],
    ).fetchall()
    return {"count": len(rows), "artists": [dict(r) for r in rows]}


def get_artist_tracks(ctx: ToolContext, artist: str, sort: str = "rating", limit: int = 50) -> dict:
    return search_tracks(ctx, artists=[artist], sort=sort, limit=limit)


def sample_tracks(ctx: ToolContext, n: int = 30, **filters) -> dict:
    """A random sample under the same filters as search_tracks - breaks top-N ruts."""
    filters.pop("sort", None)
    filters.pop("limit", None)
    return search_tracks(ctx, sort="random", limit=n, **filters)


# --------------------------------------------------------------------------- #
# get_similar_artists  (intersected with what you actually own)
# --------------------------------------------------------------------------- #
def get_similar_artists(ctx: ToolContext, artist: str, in_library_only: bool = True) -> dict:
    row = ctx.conn.execute(
        _OWNED_ARTIST.replace("SELECT 1", "SELECT plex_key"), (artist.strip().lower(),)
    ).fetchone()
    if not row:
        return {"artist": artist, "found": False, "similar": []}

    sims = ctx.conn.execute(
        "SELECT similar_name, score FROM artist_similar WHERE artist_key = ? ORDER BY score DESC",
        (row["plex_key"],),
    ).fetchall()

    result = []
    for s in sims:
        owned = ctx.conn.execute(_OWNED_ARTIST, (s["similar_name"].lower(),)).fetchone()
        if in_library_only and not owned:
            continue
        result.append({"name": s["similar_name"], "score": s["score"], "in_library": bool(owned)})
    return {"artist": artist, "found": True, "similar": result}


# --------------------------------------------------------------------------- #
# get_track
# --------------------------------------------------------------------------- #
def get_track(ctx: ToolContext, plex_key: str) -> dict:
    t = ctx.conn.execute(
        f"SELECT * FROM tracks t WHERE t.plex_key = ? AND {LIVE_TRACK_SQL}", (str(plex_key),)
    ).fetchone()
    if not t:
        return {"found": False}
    tags = ctx.conn.execute(
        "SELECT source, tag, weight FROM track_tags WHERE track_id = ? ORDER BY weight DESC",
        (t["id"],),
    ).fetchall()
    d = dict(t)
    d["found"] = True
    d["tags"] = [{"source": r["source"], "tag": r["tag"], "weight": r["weight"]} for r in tags]
    return d


# --------------------------------------------------------------------------- #
# build_playlist_preview
# --------------------------------------------------------------------------- #
def build_playlist_preview(ctx: ToolContext, plex_keys: list[str]) -> dict:
    if not plex_keys:
        return {"error": "no tracks supplied"}
    plex_keys = [str(k) for k in plex_keys]
    placeholders = ",".join("?" * len(plex_keys))
    rows = ctx.conn.execute(
        f"SELECT plex_key, title, COALESCE(track_artist, artist_name) AS artist_name, year, duration_ms "
        f"FROM tracks t WHERE plex_key IN ({placeholders}) AND {LIVE_TRACK_SQL}",
        plex_keys,
    ).fetchall()

    found = {r["plex_key"] for r in rows}
    missing = [k for k in plex_keys if k not in found]

    total_ms = sum(r["duration_ms"] or 0 for r in rows)
    by_artist: dict[str, int] = {}
    by_decade: dict[int, int] = {}
    seen: dict[str, int] = {}
    for r in rows:
        by_artist[r["artist_name"]] = by_artist.get(r["artist_name"], 0) + 1
        if r["year"]:
            dec = (r["year"] // 10) * 10
            by_decade[dec] = by_decade.get(dec, 0) + 1
        key = f"{(r['artist_name'] or '').lower()}|{(r['title'] or '').lower()}"
        seen[key] = seen.get(key, 0) + 1

    over = {a: n for a, n in by_artist.items() if n > max(3, len(rows) // 8)}
    dupes = [k for k, n in seen.items() if n > 1]

    return {
        "track_count": len(rows),
        "runtime_min": round(total_ms / 60000, 1),
        "artists": len(by_artist),
        "artist_distribution": dict(sorted(by_artist.items(), key=lambda x: -x[1])),
        "decade_distribution": dict(sorted(by_decade.items())),
        "warnings": {
            "missing_keys": missing,
            "over_represented_artists": over,
            "duplicate_tracks": dupes,
        },
    }


# --------------------------------------------------------------------------- #
# create_plex_playlist  (the only write)
# --------------------------------------------------------------------------- #
def create_plex_playlist(
    ctx: ToolContext,
    title: str,
    plex_keys: list[str],
    description: str | None = None,
) -> dict:
    if not ctx.allow_writes or ctx.plex_factory is None:
        return {"error": "writes are disabled in this session"}
    if not title.strip():
        return {"error": "title is required"}
    if not plex_keys:
        return {"error": "plex_keys is empty"}
    # Grounding gate: only keys that exist in the live library reach Plex.
    preview = build_playlist_preview(ctx, plex_keys)
    missing = set(preview["warnings"]["missing_keys"])
    keys = [str(k) for k in plex_keys if str(k) not in missing]
    if not keys:
        return {"error": "none of the supplied plex_keys are in the library"}
    with ctx.plex_factory() as plex:
        result = create_playlist(plex, title.strip(), keys, description)
    # Register it so the web UI can later replace it in place.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ctx.conn.execute(
        "INSERT OR IGNORE INTO managed_playlists(plex_playlist_key, title, track_count, created_at, updated_at) "
        "VALUES(?,?,?,?,?)", (result["ratingKey"], title.strip(), len(keys), now, now),
    )
    ctx.conn.commit()
    return {"ok": True, "dropped_unknown_keys": sorted(missing), **result}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
_FUNCS = {
    "list_facets": list_facets,
    "list_genres": list_genres,
    "list_artists": list_artists,
    "get_artist_tracks": get_artist_tracks,
    "sample_tracks": sample_tracks,
    "search_tags": search_tags,
    "search_tracks": search_tracks,
    "get_similar_artists": get_similar_artists,
    "get_track": get_track,
    "build_playlist_preview": build_playlist_preview,
    "create_plex_playlist": create_plex_playlist,
}


def dispatch(ctx: ToolContext, name: str, arguments: dict) -> dict:
    fn = _FUNCS.get(name)
    if not fn:
        return {"error": f"unknown tool {name!r}"}
    arguments = arguments or {}
    valid = _schema_params(name)
    unknown = sorted(set(arguments) - set(valid)) if valid is not None else []
    if unknown:
        # Small models often guess names (tags= for any_tags=). Say exactly what's allowed.
        return {"error": f"unknown argument(s) for {name}: {', '.join(unknown)}. "
                         f"Valid arguments: {', '.join(valid) or '(none)'}"}
    try:
        return fn(ctx, **arguments)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}. Valid arguments: {', '.join(valid or [])}"}
    except Exception as e:  # keep the agent loop alive
        return {"error": f"{type(e).__name__}: {e}"}


def _schema_params(name: str) -> list[str] | None:
    for t in TOOL_SCHEMAS:
        if t["function"]["name"] == name:
            return list(t["function"]["parameters"].get("properties", {}))
    return None


_FILTER_PROPS = {
    "any_tags": {"type": "array", "items": {"type": "string"}, "description": "match ANY of these tags"},
    "all_tags": {"type": "array", "items": {"type": "string"}, "description": "must have ALL of these tags"},
    "exclude_tags": {"type": "array", "items": {"type": "string"}},
    "artists": {"type": "array", "items": {"type": "string"}},
    "exclude_artists": {"type": "array", "items": {"type": "string"}},
    "year_min": {"type": "integer"},
    "year_max": {"type": "integer"},
    "min_user_rating": {"type": "number", "description": "Plex scale 0-10; 8 = 4 stars"},
    "min_play_count": {"type": "integer"},
    "max_play_count": {"type": "integer"},
    "never_played": {"type": "boolean"},
    "duration_min_sec": {"type": "integer"},
    "duration_max_sec": {"type": "integer"},
    "exclude_keys": {"type": "array", "items": {"type": "string"},
                     "description": "plex_keys to leave out, e.g. tracks already in the draft"},
}

# JSON-schema descriptions for OpenAI/Ollama-style function calling.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_facets",
            "description": "Summarize the user's library: totals, the most common tags "
            "(genre/mood/style/folksonomy), and tracks per decade. Call this FIRST.",
            "parameters": {
                "type": "object",
                "properties": {"top": {"type": "integer", "default": 60}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tags",
            "description": "Find tags in the library whose text contains a substring, with track counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tracks",
            "description": "Return candidate tracks that match tag / artist / year / rating / "
            "play-count / duration filters. Use repeatedly to assemble a pool, then curate.",
            "parameters": {
                "type": "object",
                "properties": {
                    **_FILTER_PROPS,
                    "boost_tags": {"type": "array", "items": {"type": "string"},
                                   "description": "don't filter, just rank tracks with these tags (e.g. moods) higher"},
                    "sort": {
                        "type": "string",
                        "enum": list(_SORTS.keys()),
                        "default": "relevance",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_genres",
            "description": "Tags in the library with track counts, optionally from one source: "
            "plex (Plex genres/styles/moods), lastfm (folksonomy) or musicbrainz.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["any", "plex", "lastfm", "musicbrainz"]},
                    "top": {"type": "integer", "default": 80},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_artists",
            "description": "Artists in the library (most tracks first), optionally filtered by tags, "
            "year range or a name substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "year_min": {"type": "integer"},
                    "year_max": {"type": "integer"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_artist_tracks",
            "description": "Tracks by one artist in the library.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "sort": {"type": "string", "enum": list(_SORTS.keys()), "default": "rating"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["artist"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sample_tracks",
            "description": "A random sample of tracks matching the same filters as search_tracks. "
            "Use for variety beyond the top-ranked results.",
            "parameters": {
                "type": "object",
                "properties": {**_FILTER_PROPS, "n": {"type": "integer", "default": 30}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_similar_artists",
            "description": "Similar artists to a given artist (Last.fm graph), by default only "
            "those present in the user's library.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "in_library_only": {"type": "boolean", "default": True},
                },
                "required": ["artist"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_track",
            "description": "Full detail for one track including every tag.",
            "parameters": {
                "type": "object",
                "properties": {"plex_key": {"type": "string"}},
                "required": ["plex_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_playlist_preview",
            "description": "Given a candidate list of plex_keys, report runtime, artist/decade "
            "balance, duplicates and over-represented artists. Use before creating.",
            "parameters": {
                "type": "object",
                "properties": {"plex_keys": {"type": "array", "items": {"type": "string"}}},
                "required": ["plex_keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plex_playlist",
            "description": "Create a new playlist on the Plex server from an ordered list "
            "of plex_keys. Only call after the user has approved the tracklist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "plex_keys": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                },
                "required": ["title", "plex_keys"],
            },
        },
    },
]
