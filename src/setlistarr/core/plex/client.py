"""Minimal Plex Media Server HTTP client (JSON).

Deliberately not python-plexapi: plexapi objects built from /all listings are "partial",
and reading any attribute that happens to be None (userRating on an unrated track,
originalTitle, ...) silently reloads the item - one HTTP request per track during a sync.
Reading the JSON directly is faster, predictable, and trivial to fake in tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from ... import __version__

ARTIST, ALBUM, TRACK = 8, 9, 10
PAGE_SIZE = 1000
METADATA_BATCH = 100


class PlexError(Exception):
    """A Plex failure with a message fit for the UI."""


class PlexNotFound(PlexError):
    """HTTP 404 - e.g. a playlist that was deleted in Plex."""


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "X-Plex-Token": token,
                "Accept": "application/json",
                "X-Plex-Product": "Setlistarr",
                "X-Plex-Version": __version__,
                "X-Plex-Client-Identifier": "setlistarr",
            },
        )
        self._machine_id: str | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PlexClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def request(
        self, method: str, path: str, params: dict | None = None, *, content: bytes | None = None
    ) -> dict[str, Any]:
        try:
            resp = self._http.request(method, path, params=params, content=content)
        except httpx.TimeoutException as e:
            raise PlexError(f"Plex timed out at {self.base_url} ({method} {path})") from e
        except httpx.TransportError as e:
            raise PlexError(f"Plex unreachable at {self.base_url}: {e}") from e
        if resp.status_code == 401:
            raise PlexError("Plex rejected the token (401)")
        if resp.status_code == 404:
            raise PlexNotFound(f"Plex returned 404 for {method} {path}")
        if resp.status_code >= 400:
            raise PlexError(f"Plex returned HTTP {resp.status_code} for {method} {path}")
        if not resp.content:
            return {}
        try:
            return resp.json().get("MediaContainer", {})
        except ValueError as e:
            raise PlexError(f"Plex sent a non-JSON response for {path}") from e

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        return self.request("GET", path, params)

    # ------------------------------------------------------------------ #
    def identity(self) -> dict[str, Any]:
        mc = self.get("/")
        self._machine_id = mc.get("machineIdentifier")
        return {
            "machine_id": self._machine_id,
            "name": mc.get("friendlyName"),
            "version": mc.get("version"),
        }

    @property
    def machine_id(self) -> str:
        if not self._machine_id:
            self.identity()
        return self._machine_id or ""

    def music_sections(self) -> list[dict[str, Any]]:
        mc = self.get("/library/sections")
        return [
            {"section_id": int(d["key"]), "title": d.get("title", "")}
            for d in mc.get("Directory", [])
            if d.get("type") == "artist"
        ]

    def count(self, section_id: int, libtype: int) -> int:
        mc = self.get(
            f"/library/sections/{section_id}/all",
            {"type": libtype, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
        )
        return int(mc.get("totalSize", mc.get("size", 0)))

    def iter_items(
        self, section_id: int, libtype: int, filters: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Page through /library/sections/{id}/all. filters use Plex syntax keys, e.g.
        {"updatedAt>>": 1700000000} -> updatedAt>>=1700000000."""
        start = 0
        while True:
            params = {
                "type": libtype,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
                **(filters or {}),
            }
            mc = self.get(f"/library/sections/{section_id}/all", params)
            items = mc.get("Metadata", [])
            yield from items
            start += len(items)
            total = int(mc.get("totalSize", start))
            if not items or start >= total:
                return

    def metadata(self, keys: list[str]) -> Iterator[dict[str, Any]]:
        """Full metadata (all Genre/Style/Mood tags) for many items, METADATA_BATCH per request."""
        for i in range(0, len(keys), METADATA_BATCH):
            batch = keys[i : i + METADATA_BATCH]
            yield from self.get(f"/library/metadata/{','.join(batch)}").get("Metadata", [])


def client_from_settings(settings) -> PlexClient:
    url, token = settings.get("plex_url"), settings.get("plex_token")
    if not url or not token:
        raise PlexError("Plex is not configured - set the server URL and token in Settings")
    return PlexClient(url, token, timeout=settings.get("plex_timeout_sec"))
