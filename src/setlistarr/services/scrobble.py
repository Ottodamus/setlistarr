"""Send Plex music plays to ListenBrainz (opt-in).

Reads Plex's play history (GET /status/sessions/history/all - plexapi PlexServer.history) for ONE
Plex account and the enabled music sections, enriches each play from the library cache (track
artist, duration, MusicBrainz artist id) and submits them as 'import' listens. A watermark
(last viewedAt sent) makes it incremental; the first run sends the whole history Plex keeps.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import __version__
from ..config import Settings
from ..core.db import get_meta, set_meta
from ..core.listenbrainz import ListenBrainzClient
from ..core.plex.client import PlexClient

PAGE = 500


def _history(plex: PlexClient, section_id: int, account_id: int, since: int | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"sort": "viewedAt:asc", "librarySectionID": section_id, "accountID": account_id,
                                  "X-Plex-Container-Start": len(out), "X-Plex-Container-Size": PAGE}
        if since:
            params["viewedAt>"] = since
        mc = plex.get("/status/sessions/history/all", params)
        page = mc.get("Metadata", []) or []
        out += page
        total = int(mc.get("totalSize", len(out)) or len(out))
        if not page or len(out) >= total:
            return out


def build_listens(conn: sqlite3.Connection, plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = [str(p.get("ratingKey")) for p in plays if p.get("ratingKey")]
    cache: dict[str, sqlite3.Row] = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        for r in conn.execute(
            f"SELECT t.plex_key, t.title, t.artist_name, t.track_artist, t.album_title, t.duration_ms, t.track_num, "
            f"a.mbid FROM tracks t LEFT JOIN artists a ON a.plex_key = t.artist_key "
            f"WHERE t.plex_key IN ({','.join('?' * len(chunk))})", chunk):
            cache[r["plex_key"]] = r
    listens = []
    for p in plays:
        if p.get("type") not in (None, "track") or not p.get("viewedAt"):
            continue
        t = cache.get(str(p.get("ratingKey")))
        artist = (t["track_artist"] or t["artist_name"]) if t else (p.get("originalTitle") or p.get("grandparentTitle"))
        track = (t["title"] if t else None) or p.get("title")
        if not artist or not track:
            continue
        info: dict[str, Any] = {"submission_client": "Setlistarr", "submission_client_version": __version__,
                                "media_player": "Plex"}
        if t and t["duration_ms"]:
            info["duration_ms"] = int(t["duration_ms"])
        if t and t["mbid"] and not t["track_artist"]:  # the MBID is the album artist's
            info["artist_mbids"] = [t["mbid"]]
        num = (t["track_num"] if t else None) or p.get("index")
        if num:
            info["tracknumber"] = str(num)
        meta: dict[str, Any] = {"artist_name": artist, "track_name": track, "additional_info": info}
        album = (t["album_title"] if t else None) or p.get("parentTitle")
        if album:
            meta["release_name"] = album
        listens.append({"listened_at": int(p["viewedAt"]), "track_metadata": meta})
    return listens


def scrobble(conn: sqlite3.Connection, plex: PlexClient, *, lb: ListenBrainzClient | None = None) -> dict[str, Any] | None:
    """Returns None when scrobbling is off; otherwise {"sent": n}. Raises on API errors."""
    s = Settings(conn)
    if not s.get("scrobble_enabled"):
        return None
    token = s.get("listenbrainz_token")
    if not token and lb is None:
        return {"error": "Scrobbling is on but no ListenBrainz token is set"}
    since = get_meta(conn, "scrobble_watermark")
    account = int(s.get("scrobble_plex_account_id") or 1)
    plays: list[dict[str, Any]] = []
    for lib in conn.execute("SELECT section_id FROM libraries WHERE enabled = 1").fetchall():
        plays += _history(plex, lib["section_id"], account, int(since) if since else None)
    plays.sort(key=lambda p: int(p.get("viewedAt") or 0))
    listens = build_listens(conn, plays)
    if not listens:
        return {"sent": 0}
    own = lb is None
    lb = lb or ListenBrainzClient(token)
    try:
        sent = lb.submit_import(listens)
    finally:
        if own:
            lb.close()
    set_meta(conn, "scrobble_watermark", str(max(int(p["viewedAt"]) for p in plays if p.get("viewedAt"))))
    return {"sent": sent}
