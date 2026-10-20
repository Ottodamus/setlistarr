"""Fake Lidarr, ListenBrainz, MusicBrainz and Last.fm on one pytest-httpserver instance.

Shapes follow Lidarr's openapi.json, the ListenBrainz docs and live ListenBrainz/MusicBrainz responses
(see core/lidarr.py and core/listenbrainz.py). Every request is recorded for assertions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from werkzeug.wrappers import Request, Response

LIDARR_KEY = "lidarr-test-key"
LB_TOKEN = "lb-test-token"


def _json(data, status=200):
    return Response(json.dumps(data), status, content_type="application/json")


@dataclass
class FakeServices:
    # Lidarr
    lidarr_artists: list[dict] = field(default_factory=list)       # already in Lidarr
    lidarr_catalog: dict[str, dict] = field(default_factory=dict)   # mbid -> lookup template
    lidarr_added: list[dict] = field(default_factory=list)          # POST /artist bodies
    # ListenBrainz
    lb_recs: list[dict] = field(default_factory=list)
    lb_meta: dict[str, dict] = field(default_factory=dict)
    lb_popular: dict[str, list[dict]] = field(default_factory=dict)
    lb_submitted: list[dict] = field(default_factory=list)
    # MusicBrainz / Last.fm
    mb_artists: dict[str, str] = field(default_factory=dict)        # lower name -> mbid
    mb_release_groups: dict[tuple[str, str], str] = field(default_factory=dict)  # (title lower, artist mbid) -> rg id
    lastfm_top_albums: dict[str, list[str]] = field(default_factory=dict)        # artist name lower -> titles
    log: list[str] = field(default_factory=list)

    def install(self, server) -> str:
        server.expect_request(re.compile(r".*")).respond_with_handler(self._handle)
        return server.url_for("/").rstrip("/")

    def add_catalog_artist(self, mbid: str, name: str) -> None:
        self.lidarr_catalog[mbid] = {"foreignArtistId": mbid, "artistName": name, "artistType": "Group",
                                     "status": "continuing", "images": [], "links": []}

    def _handle(self, req: Request) -> Response:
        self.log.append(f"{req.method} {req.full_path}")
        p = req.path
        if p.startswith("/api/v1/"):
            return self._lidarr(req, p[len("/api/v1"):])
        if p.startswith("/1/"):
            return self._lb(req, p)
        if p.startswith("/ws/2/"):
            return self._mb(req, p)
        if p.startswith("/2.0"):
            return self._lastfm(req)
        return Response("not found", 404)

    # ------------------------------------------------------------------ Lidarr
    def _lidarr(self, req: Request, p: str) -> Response:
        if req.headers.get("X-Api-Key") != LIDARR_KEY:
            return Response("Unauthorized", 401)
        if p == "/system/status":
            return _json({"appName": "Lidarr", "version": "3.0.0.0"})
        if p == "/rootfolder":
            return _json([{"id": 1, "path": "/music", "freeSpace": 2_000_000_000_000}])
        if p == "/qualityprofile":
            return _json([{"id": 1, "name": "Any"}, {"id": 2, "name": "Lossless"}])
        if p == "/metadataprofile":
            return _json([{"id": 1, "name": "Standard"}])
        if p == "/artist" and req.method == "GET":
            return _json(self.lidarr_artists)
        if p == "/artist/lookup":
            term = req.args.get("term", "")
            mbid = term.split(":", 1)[1] if term.startswith("lidarr:") else None
            return _json([self.lidarr_catalog[mbid]] if mbid in self.lidarr_catalog else [])
        if p == "/artist" and req.method == "POST":
            body = req.get_json()
            if any(a["foreignArtistId"] == body["foreignArtistId"] for a in self.lidarr_artists):
                return _json([{"propertyName": "ForeignArtistId", "errorMessage": "This artist has already been added."}], 400)
            for k in ("qualityProfileId", "metadataProfileId", "rootFolderPath", "foreignArtistId"):
                if not body.get(k):
                    return _json([{"propertyName": k, "errorMessage": f"'{k}' must not be empty."}], 400)
            created = {**body, "id": 100 + len(self.lidarr_added)}
            self.lidarr_added.append(body)
            self.lidarr_artists.append(created)
            return _json(created, 201)
        return Response("not found", 404)

    # ------------------------------------------------------------- ListenBrainz
    def _lb(self, req: Request, p: str) -> Response:
        auth = req.headers.get("Authorization", "")
        if p == "/1/validate-token":
            if not auth:
                return _json({"code": 400, "error": "You need to provide an Authorization token."}, 400)
            ok = auth == f"Token {LB_TOKEN}"
            return _json({"code": 200, "valid": ok, "message": "Token valid." if ok else "Token invalid.",
                          **({"user_name": "lbuser"} if ok else {})})
        m = re.fullmatch(r"/1/cf/recommendation/user/([^/]+)/recording", p)
        if m:
            if not self.lb_recs:
                return Response("", 204)
            return _json({"payload": {"entity": "recording", "mbids": self.lb_recs, "user_name": m.group(1),
                                      "count": len(self.lb_recs)}})
        if p == "/1/metadata/recording/" and req.method == "POST":
            ids = req.get_json()["recording_mbids"]
            return _json({i: self.lb_meta[i] for i in ids if i in self.lb_meta})
        m = re.fullmatch(r"/1/popularity/top-release-groups-for-artist/([\w-]+)", p)
        if m:
            if not auth:
                return _json({"code": 401, "error": "you need to provide an Auth token for this endpoint"}, 401)
            return _json(self.lb_popular.get(m.group(1), []))
        if p == "/1/submit-listens":
            if auth != f"Token {LB_TOKEN}":
                return _json({"code": 401, "error": "Invalid authorization token."}, 401)
            body = req.get_json()
            assert body["listen_type"] == "import" and 1 <= len(body["payload"]) <= 1000
            self.lb_submitted += body["payload"]
            return _json({"status": "ok"})
        return Response("not found", 404)

    # -------------------------------------------------------- MusicBrainz / Last.fm
    def _mb(self, req: Request, p: str) -> Response:
        q = req.args.get("query", "")
        if p == "/ws/2/artist/":
            name = re.search(r'artist:"([^"]+)"', q).group(1).lower()
            mbid = self.mb_artists.get(name)
            return _json({"artists": [{"id": mbid, "name": name, "score": 100}] if mbid else []})
        if p == "/ws/2/release-group/":
            title = re.search(r'releasegroup:"([^"]+)"', q).group(1).lower()
            arid = re.search(r"arid:([\w-]+)", q).group(1)
            rg = self.mb_release_groups.get((title, arid))
            return _json({"release-groups": [{"id": rg, "title": title.title(), "primary-type": "Album", "score": 100}] if rg else []})
        return Response("not found", 404)

    def _lastfm(self, req: Request) -> Response:
        if req.args.get("method") == "artist.gettopalbums":
            titles = self.lastfm_top_albums.get(req.args.get("artist", "").lower(), [])
            return _json({"topalbums": {"album": [{"name": t, "playcount": 1000 - i} for i, t in enumerate(titles)]}})
        return _json({})

    def requests_matching(self, pattern: str) -> list[str]:
        return [r for r in self.log if re.search(pattern, r)]
