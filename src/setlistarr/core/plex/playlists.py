"""Plex playlist writes over plain HTTP.

Request shapes verified against python-plexapi 4.18 (playlist.py: _create, addItems,
removeItems/_getPlaylistItemID, items; mixins/edit.py: editField). The bulk clear
(DELETE /playlists/{id}/items) isn't in plexapi; it's what Plex Web uses, so we verify the
result and fall back to plexapi's per-item delete if anything is left.

Callers (services.push) are responsible for only ever passing playlists Setlistarr created.
"""

from __future__ import annotations

from typing import Any

from .client import PlexClient, PlexError, PlexNotFound

ADD_BATCH = 200  # keys per PUT; keeps the uri query string a sane length


def _uri(plex: PlexClient, keys: list[str]) -> str:
    return f"server://{plex.machine_id}/com.plexapp.plugins.library/library/metadata/{','.join(keys)}"


def _edit_args(title: str | None, summary: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if title is not None:
        args.update({"title.value": title, "title.locked": 1})
    if summary is not None:
        args.update({"summary.value": summary, "summary.locked": 1})
    return args


def create_playlist(
    plex: PlexClient, title: str, keys: list[str], summary: str | None = None
) -> dict[str, Any]:
    if not keys:
        raise PlexError("cannot create an empty playlist")
    mc = plex.request(
        "POST", "/playlists",
        {"uri": _uri(plex, keys[:ADD_BATCH]), "type": "audio", "title": title, "smart": 0},
    )
    meta = (mc.get("Metadata") or [{}])[0]
    rating_key = str(meta.get("ratingKey") or "")
    if not rating_key:
        raise PlexError("Plex did not return the new playlist's ratingKey")
    for i in range(ADD_BATCH, len(keys), ADD_BATCH):
        add_items(plex, rating_key, keys[i : i + ADD_BATCH])
    if summary:
        plex.request("PUT", f"/playlists/{rating_key}", _edit_args(None, summary))
    return {"ratingKey": rating_key, "title": meta.get("title", title), "track_count": len(keys)}


def get_playlist(plex: PlexClient, rating_key: str) -> dict[str, Any] | None:
    try:
        mc = plex.get(f"/playlists/{rating_key}")
    except PlexNotFound:
        return None
    meta = (mc.get("Metadata") or [None])[0]
    return meta


def playlist_items(plex: PlexClient, rating_key: str) -> list[dict[str, Any]]:
    return plex.get(f"/playlists/{rating_key}/items").get("Metadata", []) or []


def add_items(plex: PlexClient, rating_key: str, keys: list[str]) -> None:
    for i in range(0, len(keys), ADD_BATCH):
        plex.request("PUT", f"/playlists/{rating_key}/items", {"uri": _uri(plex, keys[i : i + ADD_BATCH])})


def clear_playlist(plex: PlexClient, rating_key: str) -> str:
    """Empty a playlist. Returns 'bulk' or 'per-item' (which method actually worked)."""
    try:
        plex.request("DELETE", f"/playlists/{rating_key}/items")
    except PlexNotFound:
        pass  # older servers: no bulk clear; fall through to per-item
    remaining = playlist_items(plex, rating_key)
    if not remaining:
        return "bulk"
    for item in remaining:
        pid = item.get("playlistItemID")
        if pid is not None:
            plex.request("DELETE", f"/playlists/{rating_key}/items/{pid}")
    if playlist_items(plex, rating_key):
        raise PlexError("Plex didn't empty the playlist; it was left unchanged apart from removed items")
    return "per-item"


def replace_playlist(
    plex: PlexClient, rating_key: str, keys: list[str], *, title: str | None = None, summary: str | None = None
) -> dict[str, Any]:
    """Replace contents (and optionally title/summary) in place, keeping the ratingKey so
    Plexamp pins and playlist artwork survive. Raises PlexNotFound if it no longer exists."""
    if not keys:
        raise PlexError("refusing to replace a playlist with nothing")
    if get_playlist(plex, rating_key) is None:
        raise PlexNotFound(f"playlist {rating_key} no longer exists in Plex")
    method = clear_playlist(plex, rating_key)
    add_items(plex, rating_key, keys)
    edits = _edit_args(title, summary)
    if edits:
        plex.request("PUT", f"/playlists/{rating_key}", edits)
    return {"ratingKey": rating_key, "track_count": len(keys), "clear_method": method}


def upload_poster(plex: PlexClient, rating_key: str, image: bytes) -> None:
    """Set a playlist's poster from raw image bytes (plexapi PosterMixin.uploadPoster(filepath=...):
    POST /library/metadata/{ratingKey}/posters with the file as the body)."""
    plex.request("POST", f"/library/metadata/{rating_key}/posters", content=image)


def web_url(plex_url: str, machine_id: str, rating_key: str) -> str:
    return (
        f"{plex_url.rstrip('/')}/web/index.html#!/server/{machine_id}/playlist"
        f"?key=%2Fplaylists%2F{rating_key}"
    )
