"""Enrich the local library with external tags and similar-artist data.

Sources:
  * Last.fm  - artist/track folksonomy tags + similar artists (needs LASTFM_API_KEY)
  * MusicBrainz - canonical genres + MBID resolution (needs a contact string)

Successful (and definitive 404) responses are cached in enrich_cache so re-runs are cheap
and stay within each service's rate limits. Failures (rate limits, 5xx, network) are never
cached - they're retried with backoff and otherwise simply re-attempted on the next run.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from dataclasses import dataclass

import requests

from .. import __version__


@dataclass(frozen=True)
class EnrichConfig:
    lastfm_api_key: str | None
    musicbrainz_contact: str
    track_tags: bool = True

    @classmethod
    def from_settings(cls, settings) -> "EnrichConfig":
        return cls(
            lastfm_api_key=settings.get("lastfm_api_key") or None,
            musicbrainz_contact=settings.get("musicbrainz_contact") or "setlistarr@localhost",
            track_tags=settings.get("enrich_track_tags"),
        )

LASTFM_ROOT = "https://ws.audioscrobbler.com/2.0/"
MB_ROOT = "https://musicbrainz.org/ws/2/"
CACHE_TTL = timedelta(days=30)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MB_MIN_SCORE = 90  # MusicBrainz search score; below this the top hit is probably a different artist


class _Http:
    def __init__(self, conn: sqlite3.Connection, cfg: EnrichConfig):
        self.conn = conn
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"setlistarr/{__version__} ( {cfg.musicbrainz_contact} )"
        self._last_mb_call = 0.0

    def get_json(self, url: str, params: dict, *, rate_limit: float = 0.0) -> dict | None:
        cache_key = url + "?" + urlencode(sorted((k, v) for k, v in params.items() if k != "api_key"))
        row = self.conn.execute(
            "SELECT fetched_at, payload FROM enrich_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        # Rows with a NULL payload are legacy cached failures (plex-dj cached those): refetch.
        if row and row["payload"] is not None:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched < CACHE_TTL:
                return json.loads(row["payload"]) or None

        payload, definitive = None, False
        for attempt in range(MAX_ATTEMPTS):
            if rate_limit:
                wait = rate_limit - (time.monotonic() - self._last_mb_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_mb_call = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=20)
            except requests.RequestException:
                time.sleep(2**attempt)
                continue
            if resp.status_code in RETRY_STATUSES:
                retry_after = resp.headers.get("Retry-After", "")
                time.sleep(float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1))
                continue
            if resp.ok:
                try:
                    payload, definitive = resp.json(), True
                except ValueError:
                    pass
            elif resp.status_code == 404:
                payload, definitive = {}, True
            break
        if not definitive:
            return None

        self.conn.execute(
            "INSERT INTO enrich_cache(cache_key, fetched_at, payload) VALUES(?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
            (cache_key, datetime.now(timezone.utc).isoformat(), json.dumps(payload)),
        )
        self.conn.commit()
        return payload or None


def _lastfm_tags(http: _Http, cfg: EnrichConfig, method: str, params: dict) -> list[tuple[str, float]]:
    if not cfg.lastfm_api_key:
        return []
    q = {"method": method, "api_key": cfg.lastfm_api_key, "format": "json", **params}
    data = http.get_json(LASTFM_ROOT, q)
    if not data:
        return []
    tags = data.get("toptags", {}).get("tag", [])
    out = []
    for t in tags:
        name = (t.get("name") or "").strip().lower()
        count = float(t.get("count") or 0)
        if name:
            out.append((name, min(count, 100.0) / 100.0))
    return out[:15]


def _lastfm_similar(http: _Http, cfg: EnrichConfig, artist: str) -> list[tuple[str, str | None, float]]:
    if not cfg.lastfm_api_key:
        return []
    q = {
        "method": "artist.getsimilar",
        "artist": artist,
        "api_key": cfg.lastfm_api_key,
        "format": "json",
        "limit": 40,
    }
    data = http.get_json(LASTFM_ROOT, q)
    if not data:
        return []
    out = []
    for a in data.get("similarartists", {}).get("artist", []):
        name = (a.get("name") or "").strip()
        if name:
            out.append((name, a.get("mbid") or None, float(a.get("match") or 0)))
    return out


def _mb_genres(http: _Http, artist: str, mbid: str | None = None) -> tuple[str | None, list[str]]:
    """MBID via search (unless known), then genres via lookup: the search endpoint doesn't
    return genres at all. Falls back to the artist's voted folksonomy tags."""
    if not mbid:
        q = {"query": f'artist:"{artist}"', "fmt": "json", "limit": 1}
        data = http.get_json(MB_ROOT + "artist/", q, rate_limit=1.1)
        if not data or not data.get("artists"):
            return None, []
        top = data["artists"][0]
        if int(top.get("score") or 0) < MB_MIN_SCORE:
            return None, []
        mbid = top.get("id")
    if not mbid:
        return None, []
    data = http.get_json(MB_ROOT + f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"}, rate_limit=1.1)
    if not data:
        return mbid, []
    genres = [g["name"].strip().lower() for g in data.get("genres", []) if g.get("name")]
    if not genres:
        tags = sorted((t for t in data.get("tags", []) if (t.get("count") or 0) > 0 and t.get("name")),
                      key=lambda t: -t["count"])
        genres = [t["name"].strip().lower() for t in tags[:10]]
    return mbid, genres


def enrich(conn: sqlite3.Connection, cfg: EnrichConfig, limit: int | None = None) -> dict[str, int]:
    http = _Http(conn, cfg)
    artists = conn.execute(
        "SELECT plex_key, name, mbid FROM artists "
        "WHERE section_id IN (SELECT section_id FROM libraries WHERE enabled = 1) ORDER BY name"
    ).fetchall()
    if limit:
        artists = artists[:limit]

    stats = {"artists_processed": 0, "tags_written": 0, "similar_written": 0}

    for art in artists:
        name = art["name"]
        artist_tags = _lastfm_tags(http, cfg, "artist.gettoptags", {"artist": name})
        mbid, mb_genres = _mb_genres(http, name, art["mbid"])
        if mbid and not art["mbid"]:
            conn.execute("UPDATE artists SET mbid = ? WHERE plex_key = ?", (mbid, art["plex_key"]))

        track_rows = conn.execute(
            "SELECT id, title FROM tracks WHERE artist_key = ? AND removed_at IS NULL", (art["plex_key"],)
        ).fetchall()

        for tr in track_rows:
            payload = [(tr["id"], "lastfm-artist", tag, w) for tag, w in artist_tags]
            payload += [(tr["id"], "musicbrainz", g, 1.0) for g in mb_genres]
            if cfg.track_tags:
                for tag, w in _lastfm_tags(
                    http, cfg, "track.gettoptags", {"artist": name, "track": tr["title"]}
                ):
                    payload.append((tr["id"], "lastfm-track", tag, w))
            conn.executemany(
                "INSERT INTO track_tags(track_id, source, tag, weight) VALUES(?,?,?,?) "
                "ON CONFLICT(track_id, source, tag) DO UPDATE SET weight=excluded.weight",
                payload,
            )
            stats["tags_written"] += len(payload)

        sim = _lastfm_similar(http, cfg, name)
        conn.executemany(
            "INSERT INTO artist_similar(artist_key, similar_name, similar_mbid, score) "
            "VALUES(?,?,?,?) ON CONFLICT(artist_key, similar_name) DO UPDATE SET score=excluded.score",
            [(art["plex_key"], s[0], s[1], s[2]) for s in sim],
        )
        stats["similar_written"] += len(sim)
        stats["artists_processed"] += 1
        conn.commit()

    return stats
