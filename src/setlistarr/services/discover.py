"""Discover: recommend artists that aren't in the library, send approved ones to Lidarr, and build a
rolling Discover Weekly/Monthly playlist from what arrives (topped up with unplayed library tracks).

Sources
  * lastfm       - Last.fm similar-artist data gathered by enrichment, ranked by how many of the
                   artists you play point at each outsider (weighted by plays and ratings).
  * listenbrainz - the user's ListenBrainz collaborative-filtering recording recommendations,
                   grouped by artist.

Safety: nothing is sent to Lidarr unless the user approves it, or turned on auto-approve.
Lidarr gets one album per artist (the most popular one), never a whole discography.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..core.db import get_meta, set_meta
from ..core.enrich import LASTFM_ROOT, MB_ROOT, EnrichConfig, _Http
from ..core.lidarr import LidarrClient, LidarrError
from ..core.listenbrainz import ListenBrainzClient, ListenBrainzError
from ..core.plex.client import PlexError
from ..core.plex.sync import LIVE_TRACK_SQL
from ..generation.draft import norm_artist
from ..logs import get_logger

log = get_logger(__name__)
_run_lock = threading.Lock()


class DiscoverError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ident_for(name: str, mbid: str | None) -> str:
    return mbid or f"name:{norm_artist(name)}"


# --------------------------------------------------------------------------- #
# what's already "yours"
# --------------------------------------------------------------------------- #
def _owned(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """(normalized artist names, artist MBIDs) present in the library."""
    names, mbids = set(), set()
    for r in conn.execute(f"SELECT DISTINCT t.artist_name, t.track_artist, a.mbid FROM tracks t "
                          f"LEFT JOIN artists a ON a.plex_key = t.artist_key WHERE {LIVE_TRACK_SQL}"):
        for n in (r["artist_name"], r["track_artist"]):
            if n:
                names.add(norm_artist(n))
        if r["mbid"]:
            mbids.add(r["mbid"])
    return names, mbids


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def candidates_lastfm(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.artist_key, s.similar_name, s.similar_mbid, s.score, a.name AS owner FROM artist_similar s "
        "JOIN artists a ON a.plex_key = s.artist_key "
        "WHERE a.section_id IN (SELECT section_id FROM libraries WHERE enabled = 1)").fetchall()
    if not rows:
        raise DiscoverError("No Last.fm similar-artist data yet. Add a Last.fm API key under Settings -> "
                            "Enrichment, then run System -> Enrich tags.")
    taste = {r["artist_key"]: (r["plays"] or 0) + 5 * (r["loved"] or 0) for r in conn.execute(
        f"SELECT t.artist_key, SUM(t.play_count) AS plays, SUM(t.user_rating >= 8) AS loved "
        f"FROM tracks t WHERE {LIVE_TRACK_SQL} GROUP BY t.artist_key")}
    owned_names, owned_mbids = _owned(conn)
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        name, mbid = r["similar_name"], r["similar_mbid"] or None
        if norm_artist(name) in owned_names or (mbid and mbid in owned_mbids):
            continue
        weight = 1 + math.log1p(taste.get(r["artist_key"], 0))
        key = norm_artist(name)
        c = agg.setdefault(key, {"name": name, "mbid": mbid, "score": 0.0, "because": Counter()})
        c["mbid"] = c["mbid"] or mbid
        c["score"] += (r["score"] or 0) * weight
        c["because"][r["owner"]] += (r["score"] or 0) * weight
    out = []
    for c in agg.values():
        out.append({"name": c["name"], "mbid": c["mbid"], "score": round(c["score"], 3), "source": "lastfm",
                    "reasons": [n for n, _ in c["because"].most_common(3)]})
    return sorted(out, key=lambda c: -c["score"])


def candidates_listenbrainz(conn: sqlite3.Connection, lb: ListenBrainzClient, user: str) -> list[dict[str, Any]]:
    if not user:
        raise DiscoverError("Set and test a ListenBrainz token under Settings -> Discover first.")
    recs = lb.recommended_recordings(user, count=100)
    if not recs:
        raise DiscoverError("ListenBrainz has no recommendations for you yet - they need a few weeks of "
                            "listening history (turn on 'Send my Plex plays to ListenBrainz').")
    meta = lb.recording_artists([r["recording_mbid"] for r in recs])
    owned_names, owned_mbids = _owned(conn)
    agg: dict[str, dict[str, Any]] = {}
    for r in recs:
        m = meta.get(r["recording_mbid"]) or {}
        artist = next((a for a in m.get("artists") or [] if a.get("artist_mbid")), None)
        if not artist:
            continue
        name, mbid = artist.get("name") or "", artist["artist_mbid"]
        if mbid in owned_mbids or norm_artist(name) in owned_names:
            continue
        c = agg.setdefault(mbid, {"name": name, "mbid": mbid, "score": 0.0, "tracks": []})
        c["score"] += r.get("score") or 0
        if m.get("name") and len(c["tracks"]) < 3:
            c["tracks"].append(m["name"])
    return sorted(({"name": c["name"], "mbid": c["mbid"], "score": round(c["score"], 4), "source": "listenbrainz",
                    "reasons": c["tracks"]} for c in agg.values()), key=lambda c: -c["score"])


def refresh(conn: sqlite3.Connection, candidates: list[dict[str, Any]], per_run: int) -> int:
    """Add up to per_run new suggestions; refresh scores of pending ones. Returns new count."""
    existing = {r["ident"]: r["status"] for r in conn.execute("SELECT ident, status FROM discoveries")}
    new = 0
    with conn:
        for c in candidates:
            ident = ident_for(c["name"], c["mbid"])
            if ident in existing:
                if existing[ident] == "suggested":
                    conn.execute("UPDATE discoveries SET score=?, reasons_json=?, source=?, updated_at=? WHERE ident=?",
                                 (c["score"], json.dumps(c["reasons"]), c["source"], _now(), ident))
                continue
            if new >= per_run:
                continue
            conn.execute(
                "INSERT INTO discoveries(ident, artist_name, artist_mbid, source, score, reasons_json, status, "
                "first_seen_at, updated_at) VALUES(?,?,?,?,?,?, 'suggested', ?, ?)",
                (ident, c["name"], c["mbid"], c["source"], c["score"], json.dumps(c["reasons"]), _now(), _now()))
            existing[ident] = "suggested"
            new += 1
    return new


# --------------------------------------------------------------------------- #
# identification + "most popular album"
# --------------------------------------------------------------------------- #
def _http(conn) -> tuple[_Http, EnrichConfig]:
    cfg = EnrichConfig.from_settings(Settings(conn))
    return _Http(conn, cfg), cfg


def mb_artist_mbid(http: _Http, name: str) -> str | None:
    data = http.get_json(MB_ROOT + "artist/", {"query": f'artist:"{name}"', "fmt": "json", "limit": 1}, rate_limit=1.1)
    top = (data or {}).get("artists") or []
    if top and int(top[0].get("score") or 0) >= 90:
        return top[0].get("id")
    return None


def popular_album(conn: sqlite3.Connection, mbid: str, name: str,
                  lb: ListenBrainzClient | None) -> tuple[str | None, str | None, str]:
    """(release_group_mbid, title, how) for the artist's most popular album.
    ListenBrainz listen counts (needs a token) -> Last.fm top album matched on MusicBrainz -> none."""
    if lb is not None and lb.has_token:
        try:
            hit = lb.top_album(mbid)
            if hit:
                return hit[0], hit[1], "ListenBrainz listen counts"
        except ListenBrainzError as e:
            log.info("discover.lb_popularity_failed", artist=name, error=str(e))
    http, cfg = _http(conn)
    if cfg.lastfm_api_key:
        data = http.get_json(LASTFM_ROOT, {"method": "artist.gettopalbums", "mbid": mbid, "artist": name,
                                           "api_key": cfg.lastfm_api_key, "format": "json", "limit": 5})
        for album in ((data or {}).get("topalbums") or {}).get("album") or []:
            title = (album.get("name") or "").strip()
            if not title or title.lower() in ("(null)", "null"):
                continue
            q = f'releasegroup:"{title}" AND arid:{mbid} AND primarytype:album'
            rg = http.get_json(MB_ROOT + "release-group/", {"query": q, "fmt": "json", "limit": 1}, rate_limit=1.1)
            groups = (rg or {}).get("release-groups") or []
            if groups and int(groups[0].get("score") or 0) >= 90:
                return groups[0]["id"], groups[0].get("title") or title, "Last.fm top album"
    return None, None, "no popularity data - Lidarr will monitor the latest album"


# --------------------------------------------------------------------------- #
# Lidarr
# --------------------------------------------------------------------------- #
def lidarr_from_settings(s: Settings) -> LidarrClient:
    url, key = s.get("lidarr_url"), s.get("lidarr_api_key")
    if not url or not key:
        raise DiscoverError("Lidarr isn't connected - add it under Settings -> Discover")
    return LidarrClient(url, key)


def _lidarr_targets(s: Settings) -> tuple[str, int, int]:
    root, qp, mp = s.get("lidarr_root_folder"), s.get("lidarr_quality_profile_id"), s.get("lidarr_metadata_profile_id")
    if not (root and qp and mp):
        raise DiscoverError("Pick Lidarr's root folder, quality profile and metadata profile under Settings -> Discover")
    return root, int(qp), int(mp)


def request(conn: sqlite3.Connection, discovery_ids: list[int], *, lidarr: LidarrClient | None = None,
            lb: ListenBrainzClient | None = None) -> list[dict[str, Any]]:
    """Send approved discoveries to Lidarr: add artist, monitor + search only its most popular album."""
    s = Settings(conn)
    root, qp, mp = _lidarr_targets(s)
    own_lidarr = lidarr is None
    lidarr = lidarr or lidarr_from_settings(s)
    own_lb = lb is None and bool(s.get("listenbrainz_token"))
    lb = lb or (ListenBrainzClient(s.get("listenbrainz_token")) if own_lb else None)
    http, _ = _http(conn)
    results = []
    try:
        in_lidarr = lidarr.artist_mbids()
        for did in discovery_ids:
            d = conn.execute("SELECT * FROM discoveries WHERE id = ?", (did,)).fetchone()
            if not d or d["status"] not in ("suggested", "failed"):
                continue
            res = _request_one(conn, dict(d), lidarr, lb, http, in_lidarr, root, qp, mp)
            results.append(res)
    finally:
        if own_lidarr:
            lidarr.close()
        if own_lb and lb:
            lb.close()
    return results


def _request_one(conn, d, lidarr, lb, http, in_lidarr, root, qp, mp) -> dict[str, Any]:
    def finish(status, note=None, **cols):
        sets = ", ".join(f"{k} = ?" for k in cols)
        with conn:
            conn.execute(f"UPDATE discoveries SET status = ?, note = ?, updated_at = ?"
                         f"{', ' + sets if sets else ''}{', requested_at = ?' if status == 'requested' else ''} WHERE id = ?",
                         (status, note, _now(), *cols.values(), *([_now()] if status == "requested" else []), d["id"]))
        return {"id": d["id"], "artist": d["artist_name"], "status": status, "note": note, **cols}

    mbid = d["artist_mbid"] or mb_artist_mbid(http, d["artist_name"])
    if not mbid:
        return finish("failed", "Couldn't identify this artist on MusicBrainz, so Lidarr can't add it")
    if mbid in in_lidarr:
        return finish("requested", "Already in Lidarr", artist_mbid=mbid, lidarr_artist_id=in_lidarr[mbid])
    try:
        template = lidarr.lookup(mbid)
        if not template:
            return finish("failed", "Lidarr couldn't find this artist in its metadata", artist_mbid=mbid)
        if d.get("album_checked_at"):  # the album shown on the Discover page is the one that's sent
            album_mbid, album_title, how = d["album_mbid"], d["album_title"], d["album_how"] or "the Discover page"
        else:
            album_mbid, album_title, how = popular_album(conn, mbid, d["artist_name"], lb)
        added = lidarr.add_artist(template, root_folder=root, quality_profile_id=qp, metadata_profile_id=mp,
                                  album_mbid=album_mbid)
    except LidarrError as e:
        if "already" in str(e).lower():
            return finish("requested", "Already in Lidarr", artist_mbid=mbid)
        return finish("failed", str(e), artist_mbid=mbid)
    log.info("discover.requested", artist=d["artist_name"], album=album_title, how=how)
    return finish("requested", f"Album chosen by {how}" if album_mbid else how.capitalize(), artist_mbid=mbid,
                  lidarr_artist_id=(added or {}).get("id"), album_mbid=album_mbid, album_title=album_title)


def resolve_albums(conn: sqlite3.Connection, lb: ListenBrainzClient | None, *, ids: list[int] | None = None) -> int:
    """Identify the artist and pick its most popular album for suggestions that don't have one yet,
    so the Discover page shows what would be downloaded. Returns how many were resolved."""
    http, _ = _http(conn)
    q = "SELECT * FROM discoveries WHERE status IN ('suggested', 'failed') AND album_checked_at IS NULL"
    rows = conn.execute(q + (f" AND id IN ({','.join('?' * len(ids))})" if ids else ""), ids or []).fetchall()
    done = 0
    for d in rows:
        mbid = d["artist_mbid"] or mb_artist_mbid(http, d["artist_name"])
        if not mbid:
            with conn:
                conn.execute("UPDATE discoveries SET album_checked_at=?, album_how=? WHERE id=?",
                             (_now(), "artist not found on MusicBrainz", d["id"]))
            continue
        album_mbid, title, how = popular_album(conn, mbid, d["artist_name"], lb)
        with conn:
            conn.execute("UPDATE discoveries SET artist_mbid=?, album_mbid=?, album_title=?, album_how=?, "
                         "album_checked_at=?, updated_at=? WHERE id=?",
                         (mbid, album_mbid, title, how, _now(), _now(), d["id"]))
        done += 1
    return done


def dismiss(conn: sqlite3.Connection, discovery_id: int, undo: bool = False) -> None:
    with conn:
        conn.execute("UPDATE discoveries SET status = ?, updated_at = ? WHERE id = ? AND status IN (?, ?)",
                     ("suggested" if undo else "dismissed", _now(), discovery_id,
                      *(("dismissed", "dismissed") if undo else ("suggested", "failed"))))


def check_arrivals(conn: sqlite3.Connection) -> int:
    """Requested artists that now have tracks in Plex become 'arrived'."""
    names, mbids = _owned(conn)
    n = 0
    with conn:
        for d in conn.execute("SELECT id, artist_name, artist_mbid FROM discoveries WHERE status = 'requested'").fetchall():
            if (d["artist_mbid"] and d["artist_mbid"] in mbids) or norm_artist(d["artist_name"]) in names:
                conn.execute("UPDATE discoveries SET status='arrived', arrived_at=?, updated_at=? WHERE id=?",
                             (_now(), _now(), d["id"]))
                n += 1
    return n


# --------------------------------------------------------------------------- #
# the Discover playlist
# --------------------------------------------------------------------------- #
def playlist_title(s: Settings) -> str:
    return (s.get("discover_playlist_title") or "").strip() or (
        "Discover Monthly" if s.get("discover_cadence") == "monthly" else "Discover Weekly")


def _taste_tags(conn, n: int = 12) -> list[str]:
    return [r["tag"] for r in conn.execute(
        f"""SELECT tt.tag, SUM(t.play_count + 3 * (t.user_rating >= 8)) AS w FROM track_tags tt
            JOIN tracks t ON t.id = tt.track_id WHERE {LIVE_TRACK_SQL} AND (t.play_count > 0 OR t.user_rating >= 8)
            GROUP BY tt.tag ORDER BY w DESC LIMIT ?""", (n,))]


_BY_ARTIST_SQL = "(LOWER(t.artist_name) = LOWER(?) OR LOWER(COALESCE(t.track_artist, '')) = LOWER(?))"


def artist_tracks(conn: sqlite3.Connection, d) -> list[dict]:
    """The arrived artist's live tracks, the album sent to Lidarr first."""
    return [dict(r) for r in conn.execute(
        f"""SELECT t.plex_key, t.title, t.album_title, t.year, t.duration_ms, t.play_count,
                   COALESCE(t.track_artist, t.artist_name) AS artist FROM tracks t
            WHERE {LIVE_TRACK_SQL} AND {_BY_ARTIST_SQL}
            ORDER BY (LOWER(t.album_title) = LOWER(COALESCE(?, ''))) DESC, t.year DESC NULLS LAST,
                     t.album_title, t.disc_num, t.track_num""",
        (d["artist_name"], d["artist_name"], d["album_title"]))]


def _auto_tracks(conn: sqlite3.Connection, d) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT t.plex_key, t.duration_ms, COALESCE(t.track_artist, t.artist_name) AS artist FROM tracks t
            WHERE {LIVE_TRACK_SQL} AND {_BY_ARTIST_SQL}
            ORDER BY (LOWER(t.album_title) = LOWER(COALESCE(?, ''))) DESC, t.user_rating DESC NULLS LAST,
                     t.disc_num, t.track_num LIMIT 3""",
        (d["artist_name"], d["artist_name"], d["album_title"]))]


def _picked_tracks(conn: sqlite3.Connection, discovery_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""SELECT t.plex_key, t.duration_ms, COALESCE(t.track_artist, t.artist_name) AS artist
            FROM discover_picks p JOIN tracks t ON t.plex_key = p.plex_key
            WHERE p.discovery_id = ? AND {LIVE_TRACK_SQL} ORDER BY p.position""", (discovery_id,))]


def compose_playlist(conn: sqlite3.Connection, *, rng: random.Random | None = None) -> dict[str, Any]:
    """Arrivals from the last N weeks (your picked songs, or up to 3 tracks per artist with the album
    sent to Lidarr first), topped up with never-played library tracks that match your most-played tags."""
    rng = rng or random.Random()
    s = Settings(conn)
    target_ms = int(s.get("discover_minutes")) * 60_000
    since = (datetime.now(timezone.utc) - timedelta(weeks=int(s.get("discover_window_weeks")))).isoformat()
    arrived = conn.execute("SELECT * FROM discoveries WHERE status = 'arrived' AND (arrived_at >= ? OR picked_at >= ?) "
                           "ORDER BY arrived_at DESC", (since, since)).fetchall()
    new_tracks: list[dict] = []
    for d in arrived:
        if d["picked_at"]:
            new_tracks += [dict(r, new=True, pinned=True) for r in _picked_tracks(conn, d["id"])]
        else:
            new_tracks += [dict(r, new=True) for r in _auto_tracks(conn, d)]
    picked = list(new_tracks)
    total = sum(t["duration_ms"] or 0 for t in picked)
    if total > target_ms * 0.75:  # leave room for the top-up only if arrivals are light
        for t in reversed(list(picked)):  # trim automatic picks only - songs you picked always stay
            if total <= target_ms:
                break
            if not t.get("pinned"):
                picked.remove(t)
                total -= t["duration_ms"] or 0
    tags = _taste_tags(conn)
    if tags and total < target_ms:
        have = {t["plex_key"] for t in picked}
        pool = [dict(r) for r in conn.execute(
            f"""SELECT DISTINCT t.plex_key, t.duration_ms, COALESCE(t.track_artist, t.artist_name) AS artist FROM tracks t
                JOIN track_tags tt ON tt.track_id = t.id
                WHERE {LIVE_TRACK_SQL} AND t.play_count = 0 AND tt.tag IN ({','.join('?' * len(tags))})""", tags)]
        rng.shuffle(pool)
        per_artist: Counter = Counter(norm_artist(t["artist"]) for t in picked)
        for t in pool:
            if total >= target_ms:
                break
            a = norm_artist(t["artist"] or "")
            if t["plex_key"] in have or per_artist[a] >= 2:
                continue
            picked.append(dict(t, new=False))
            have.add(t["plex_key"])
            per_artist[a] += 1
            total += t["duration_ms"] or 0
    return {"tracks": _spread(picked), "arrived_artists": [d["artist_name"] for d in arrived],
            "new_count": sum(1 for t in picked if t.get("new")), "total_ms": total}


def _spread(tracks: list[dict]) -> list[dict]:
    """Round-robin by artist so the same artist never plays twice in a row when avoidable."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for t in tracks:
        a = norm_artist(t["artist"] or "")
        if a not in buckets:
            order.append(a)
        buckets[a].append(t)
    out = []
    while any(buckets.values()):
        for a in order:
            if buckets[a]:
                out.append(buckets[a].pop(0))
    return out


def build_playlist(conn: sqlite3.Connection, *, plex=None, cover_inject: dict | None = None) -> dict[str, Any]:
    """Compose and push the Discover playlist (create on first run, replace in place after)."""
    from .covers import default_prompt, generate_safely, set_cover
    from .push import push_draft

    s = Settings(conn)
    comp = compose_playlist(conn)
    if not comp["tracks"]:
        return {"status": "skipped", "note": "Nothing to put in it yet - no arrivals and no unplayed tracks matched"}
    title = playlist_title(s)
    names = comp["arrived_artists"][:5]
    desc = (f"New to you: {', '.join(names)}{' and more' if len(comp['arrived_artists']) > 5 else ''}. "
            if names else "") + "Plus unplayed tracks from your library that match what you play most. Made by Setlistarr."
    now = _now()
    with conn:
        draft_id = conn.execute(
            "INSERT INTO drafts(generation_id, title, description, adjustments_json, created_at, updated_at) "
            "VALUES(NULL, ?, ?, ?, ?, ?)", (title, desc, json.dumps([]), now, now)).lastrowid
        conn.executemany("INSERT INTO draft_tracks(draft_id, position, plex_key, source) VALUES(?,?,?,?)",
                         [(draft_id, i, t["plex_key"], "discover" if t.get("new") else "library")
                          for i, t in enumerate(comp["tracks"])])
    cover_note = None
    mode = s.get("discover_cover_mode")
    prev_cover = get_meta(conn, "discover_cover_id")
    if mode == "renew" or (mode == "keep" and not prev_cover):
        cid, err = generate_safely(conn, prompt=default_prompt(title, desc), draft_id=draft_id,
                                   provider_id=s.get("discover_cover_provider_id"), inject=cover_inject)
        if cid:
            set_meta(conn, "discover_cover_id", str(cid))
            prev_cover = str(cid)
        else:
            cover_note = f"Cover wasn't generated: {err}"
    if mode != "none" and prev_cover:
        set_cover(conn, int(prev_cover), draft_id=draft_id)

    key = get_meta(conn, "discover_playlist_key")
    managed = key and conn.execute("SELECT 1 FROM managed_playlists WHERE plex_playlist_key = ?", (key,)).fetchone()
    try:
        res = push_draft(conn, draft_id, title=title, summary=desc, replace_key=key if managed else None, plex=plex)
    except PlexError as e:
        return {"status": "error", "error": str(e), "draft_id": draft_id}
    set_meta(conn, "discover_playlist_key", res["ratingKey"])
    set_meta(conn, "discover_draft_id", str(draft_id))
    return {"status": "ok", "action": res["action"], "tracks": res["track_count"], "new_tracks": comp["new_count"],
            "title": title, "draft_id": draft_id, "cover_note": cover_note or res.get("cover_error")}


def _spread_in(base: list[str], add: list[str]) -> list[str]:
    """Insert `add` evenly through `base` (ascending final positions, so earlier inserts don't shift later ones)."""
    out, n = list(base), len(base) + len(add)
    for i, k in enumerate(add):
        out.insert(min(len(out), int((i + 0.5) * n / len(add))), k)
    return out


def set_picks(conn: sqlite3.Connection, discovery_id: int, keys: list[str] | None, *, plex=None) -> dict[str, Any]:
    """Choose which of an arrived artist's songs are in the Discover playlist (keys=None = back to the
    automatic 3), then update the playlist in place right away: this artist's other songs come out, the
    picked ones go in spread through it, and everything else (and the cover) stays as it is."""
    d = conn.execute("SELECT * FROM discoveries WHERE id = ?", (discovery_id,)).fetchone()
    if not d or d["status"] != "arrived":
        raise DiscoverError("Only artists that have arrived in Plex can be added to the playlist")
    mine = {t["plex_key"] for t in artist_tracks(conn, d)}
    with conn:
        conn.execute("DELETE FROM discover_picks WHERE discovery_id = ?", (discovery_id,))
        if keys is None:
            conn.execute("UPDATE discoveries SET picked_at = NULL, updated_at = ? WHERE id = ?", (_now(), discovery_id))
            chosen = [t["plex_key"] for t in _auto_tracks(conn, d)]
        else:
            chosen = list(dict.fromkeys(k for k in keys if k in mine))  # only this artist's live tracks
            conn.executemany("INSERT INTO discover_picks(discovery_id, plex_key, position) VALUES(?,?,?)",
                             [(discovery_id, k, i) for i, k in enumerate(chosen)])
            conn.execute("UPDATE discoveries SET picked_at = ?, updated_at = ? WHERE id = ?", (_now(), _now(), discovery_id))

    did, key = get_meta(conn, "discover_draft_id"), get_meta(conn, "discover_playlist_key")
    managed = key and conn.execute("SELECT 1 FROM managed_playlists WHERE plex_playlist_key = ?", (key,)).fetchone()
    old = conn.execute("SELECT * FROM drafts WHERE id = ?", (int(did),)).fetchone() if did else None
    if not (managed and old):  # no Discover playlist yet: build the whole thing (includes the picks)
        return {**build_playlist(conn, plex=plex), "songs": len(chosen)}
    cur = conn.execute("SELECT plex_key, source FROM draft_tracks WHERE draft_id = ? ORDER BY position", (old["id"],)).fetchall()
    kept = [r["plex_key"] for r in cur if r["plex_key"] not in mine or r["plex_key"] in chosen]
    new = _spread_in(kept, [k for k in chosen if k not in kept])
    if new == [r["plex_key"] for r in cur]:
        return {"status": "ok", "action": "unchanged", "songs": len(chosen), "title": old["title"]}
    if not new:
        return {**build_playlist(conn, plex=plex), "songs": 0}
    source = {r["plex_key"]: r["source"] for r in cur}
    now = _now()
    with conn:
        draft_id = conn.execute(
            "INSERT INTO drafts(generation_id, title, description, adjustments_json, cover_id, created_at, updated_at) "
            "VALUES(NULL, ?, ?, ?, ?, ?, ?)",
            (old["title"], old["description"], json.dumps([]), old["cover_id"], now, now)).lastrowid
        conn.executemany("INSERT INTO draft_tracks(draft_id, position, plex_key, source) VALUES(?,?,?,?)",
                         [(draft_id, i, k, "discover" if k in mine else source.get(k, "library"))
                          for i, k in enumerate(new)])
    from .push import push_draft
    try:
        res = push_draft(conn, draft_id, title=old["title"], summary=old["description"], replace_key=key, plex=plex)
    except PlexError as e:
        return {"status": "error", "error": str(e), "draft_id": draft_id}
    set_meta(conn, "discover_playlist_key", res["ratingKey"])
    set_meta(conn, "discover_draft_id", str(draft_id))
    return {"status": "ok", "action": res["action"], "songs": len(chosen), "tracks": res["track_count"],
            "title": old["title"], "draft_id": draft_id}


def playlist_keys(conn: sqlite3.Connection) -> set[str]:
    did = get_meta(conn, "discover_draft_id")
    return {r[0] for r in conn.execute("SELECT plex_key FROM draft_tracks WHERE draft_id = ?", (int(did),))} if did else set()


def remember_draft_cover(conn: sqlite3.Connection) -> None:
    """If the user changed the cover on the latest Discover draft, 'keep' mode keeps that one."""
    did = get_meta(conn, "discover_draft_id")
    if did:
        row = conn.execute("SELECT cover_id FROM drafts WHERE id = ?", (int(did),)).fetchone()
        if row and row["cover_id"]:
            set_meta(conn, "discover_cover_id", str(row["cover_id"]))


# --------------------------------------------------------------------------- #
# one full run
# --------------------------------------------------------------------------- #
def run(conn: sqlite3.Connection, trigger: str = "manual", *, build: bool = True, inject: dict | None = None) -> dict[str, Any]:
    """refresh suggestions -> (auto-approve -> Lidarr) -> arrivals -> playlist. Never raises."""
    if not _run_lock.acquire(blocking=False):
        return {"status": "skipped", "error": "a Discover run is already in progress"}
    inject = inject or {}
    run_id = conn.execute("INSERT INTO discover_runs(started_at, trigger, status) VALUES(?, ?, 'running')",
                          (_now(), trigger)).lastrowid
    conn.commit()
    stats: dict[str, Any] = {}
    status, error = "ok", None
    try:
        s = Settings(conn)
        source = s.get("discover_source")
        lb = inject.get("lb") or (ListenBrainzClient(s.get("listenbrainz_token")) if s.get("listenbrainz_token") else None)
        try:
            if source == "listenbrainz":
                cands = candidates_listenbrainz(conn, lb or ListenBrainzClient(), s.get("listenbrainz_user"))
            else:
                cands = candidates_lastfm(conn)
            stats["new_suggestions"] = refresh(conn, cands, int(s.get("discover_per_run")))
            stats["albums_picked"] = resolve_albums(conn, lb)
        except (DiscoverError, ListenBrainzError) as e:
            stats["suggestions_error"] = str(e)
        if s.get("discover_auto_approve"):
            ids = [r["id"] for r in conn.execute("SELECT id FROM discoveries WHERE status = 'suggested' ORDER BY score DESC")]
            if ids:
                try:
                    res = request(conn, ids, lidarr=inject.get("lidarr"), lb=lb)
                    stats["sent_to_lidarr"] = sum(1 for r in res if r["status"] == "requested")
                    stats["failed"] = sum(1 for r in res if r["status"] == "failed")
                except (DiscoverError, LidarrError) as e:
                    stats["lidarr_error"] = str(e)
        stats["arrived"] = check_arrivals(conn)
        if build:
            remember_draft_cover(conn)
            stats["playlist"] = build_playlist(conn, plex=inject.get("plex"), cover_inject=inject.get("cover_inject"))
        if stats.get("suggestions_error") and not stats.get("playlist", {}).get("status") == "ok":
            status, error = "error", stats["suggestions_error"]
    except Exception as e:  # never kill the scheduler thread
        status, error = "error", f"{type(e).__name__}: {e}"
        log.exception("discover.failed")
    finally:
        _run_lock.release()
    with conn:
        conn.execute("UPDATE discover_runs SET finished_at=?, status=?, stats_json=?, error=? WHERE id=?",
                     (_now(), status, json.dumps(stats, default=str), error, run_id))
    log.info("discover.run", status=status, **{k: v for k, v in stats.items() if not isinstance(v, dict)})
    return {"status": status, "error": error, **stats}
