"""ListenBrainz API client. Checked against the official docs and live responses (2026-10):
  * recommendations: GET /1/cf/recommendation/user/{user}/recording (public; 204 = none yet)
  * recording metadata: POST /1/metadata/recording/ {"recording_mbids", "inc": "artist"} (public)
  * popularity: GET /1/popularity/top-release-groups-for-artist/{mbid} - needs a token since 2026
  * submit: POST /1/submit-listens, listen_type 'import', <=1000 listens/request
  * auth: "Authorization: Token <token>"; GET /1/validate-token -> {valid, user_name}
"""

from __future__ import annotations

from typing import Any

import httpx

from .. import __version__

ROOT = "https://api.listenbrainz.org"
SUBMIT_BATCH = 500


class ListenBrainzError(Exception):
    pass


class ListenBrainzClient:
    def __init__(self, token: str | None = None, *, base_url: str = ROOT, timeout: float = 30,
                 transport: httpx.BaseTransport | None = None):
        headers = {"User-Agent": f"setlistarr/{__version__} ( https://github.com/Ottodamus/setlistarr )"}
        if token:
            headers["Authorization"] = f"Token {token}"
        self.has_token = bool(token)
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ListenBrainzClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise ListenBrainzError("ListenBrainz timed out") from e
        except httpx.TransportError as e:
            raise ListenBrainzError(f"Can't reach ListenBrainz: {e}") from e
        if r.status_code == 401:
            raise ListenBrainzError("ListenBrainz rejected the token (401)")
        if r.status_code == 429:
            raise ListenBrainzError("ListenBrainz rate limit hit - try again later")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            raise ListenBrainzError(f"ListenBrainz returned {r.status_code}: {str(msg)[:200]}")
        return r

    # ------------------------------------------------------------------ #
    def validate_token(self) -> str:
        """Returns the token owner's user name, or raises."""
        if not self.has_token:
            raise ListenBrainzError("No ListenBrainz token set")
        data = self._req("GET", "/1/validate-token").json()
        if not data.get("valid"):
            raise ListenBrainzError("ListenBrainz says the token is invalid")
        return data.get("user_name") or ""

    def recommended_recordings(self, user: str, count: int = 100) -> list[dict[str, Any]]:
        r = self._req("GET", f"/1/cf/recommendation/user/{user}/recording", params={"count": count})
        if r.status_code == 204 or not r.content:
            return []  # not generated yet: needs a few weeks of listens
        return r.json().get("payload", {}).get("mbids", []) or []

    def recording_artists(self, recording_mbids: list[str]) -> dict[str, dict[str, Any]]:
        """recording_mbid -> {"name": track name, "artists": [{"artist_mbid", "name"}]}."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(recording_mbids), 50):
            batch = recording_mbids[i : i + 50]
            data = self._req("POST", "/1/metadata/recording/", json={"recording_mbids": batch, "inc": "artist"}).json()
            for mbid, meta in (data or {}).items():
                out[mbid] = {"name": (meta.get("recording") or {}).get("name"),
                             "artists": (meta.get("artist") or {}).get("artists") or []}
        return out

    def top_album(self, artist_mbid: str) -> tuple[str, str] | None:
        """(release_group_mbid, title) of the artist's most-listened album. Needs a token."""
        if not self.has_token:
            return None
        rows = self._req("GET", f"/1/popularity/top-release-groups-for-artist/{artist_mbid}").json() or []
        albums = [x for x in rows if ((x.get("release_group") or {}).get("type") or "Album") == "Album"]
        for x in albums or rows:
            if x.get("release_group_mbid"):
                return x["release_group_mbid"], (x.get("release_group") or {}).get("name") or ""
        return None

    def submit_import(self, listens: list[dict[str, Any]]) -> int:
        sent = 0
        for i in range(0, len(listens), SUBMIT_BATCH):
            batch = listens[i : i + SUBMIT_BATCH]
            self._req("POST", "/1/submit-listens", json={"listen_type": "import", "payload": batch})
            sent += len(batch)
        return sent
