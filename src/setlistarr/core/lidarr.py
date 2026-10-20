"""Minimal Lidarr v1 API client (httpx). Request shapes checked against Lidarr's openapi.json
and source: X-Api-Key header; artist lookup accepts 'lidarr:<mbid>'; AddArtistOptions.albumsToMonitor
(release-group MBIDs = foreignAlbumId) monitors exactly those albums when given."""

from __future__ import annotations

from typing import Any

import httpx

from .. import __version__


class LidarrError(Exception):
    pass


class LidarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport,
            headers={"X-Api-Key": api_key, "Accept": "application/json",
                     "User-Agent": f"setlistarr/{__version__}"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LidarrClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._http.request(method, f"/api/v1{path}", **kw)
        except httpx.TimeoutException as e:
            raise LidarrError(f"Lidarr timed out at {self.base_url}") from e
        except httpx.TransportError as e:
            raise LidarrError(f"Can't reach Lidarr at {self.base_url}: {e}") from e
        if r.status_code == 401:
            raise LidarrError("Lidarr rejected the API key (401)")
        if r.status_code >= 400:
            raise LidarrError(f"Lidarr returned {r.status_code}: {_error_text(r)}")
        return r.json() if r.content else None

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return self._req("GET", "/system/status")

    def root_folders(self) -> list[dict[str, Any]]:
        return self._req("GET", "/rootfolder") or []

    def quality_profiles(self) -> list[dict[str, Any]]:
        return self._req("GET", "/qualityprofile") or []

    def metadata_profiles(self) -> list[dict[str, Any]]:
        return self._req("GET", "/metadataprofile") or []

    def artist_mbids(self) -> dict[str, int]:
        """foreignArtistId (MBID) -> Lidarr artist id, for everything already in Lidarr."""
        return {a["foreignArtistId"]: a["id"] for a in self._req("GET", "/artist") or [] if a.get("foreignArtistId")}

    def lookup(self, mbid: str) -> dict[str, Any] | None:
        for a in self._req("GET", "/artist/lookup", params={"term": f"lidarr:{mbid}"}) or []:
            if a.get("foreignArtistId") == mbid:
                return a
        return None

    def add_artist(self, template: dict[str, Any], *, root_folder: str, quality_profile_id: int,
                   metadata_profile_id: int, album_mbid: str | None) -> dict[str, Any]:
        """Add the artist monitoring only `album_mbid` (searched immediately). Without an album,
        falls back to Lidarr's 'latest' option. Future releases are never auto-monitored."""
        body = {
            **template,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "monitorNewItems": "none",
            "addOptions": {
                # 'unknown' + albumsToMonitor = "exactly these albums"; 'none' would unmonitor the artist.
                "monitor": "unknown" if album_mbid else "latest",
                "albumsToMonitor": [album_mbid] if album_mbid else [],
                "monitored": True,
                "searchForMissingAlbums": True,
            },
        }
        return self._req("POST", "/artist", json=body)


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(data, list):  # validation errors: [{"propertyName", "errorMessage"}]
        return "; ".join(str(e.get("errorMessage", e)) for e in data)[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]
