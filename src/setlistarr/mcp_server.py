"""MCP stdio server exposing the library tools to Claude (Desktop / Code).

Run:  setlistarr-mcp   (or: setlistarr mcp)
Uses the same DB as the web app (SETLISTARR_CONFIG_DIR / SETLISTARR_DB).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .config import Settings, bootstrap
from .core.db import connect, migrate
from .core.plex.client import client_from_settings
from .core.tools import ToolContext, dispatch

mcp = MCPServer("setlistarr")
_ctx: ToolContext | None = None


def _context() -> ToolContext:
    global _ctx
    if _ctx is None:
        conn = connect(bootstrap().db_path)
        migrate(conn)
        settings = Settings(conn)
        _ctx = ToolContext(
            conn=conn, plex_factory=lambda: client_from_settings(settings), allow_writes=True
        )
    return _ctx


@mcp.tool()
def list_facets(top: int = 60) -> dict:
    """Summary of the library: totals, most common tags, tracks per decade. Call first."""
    return dispatch(_context(), "list_facets", {"top": top})


@mcp.tool()
def search_tags(query: str, limit: int = 25) -> dict:
    """Find library tags containing a substring, with track counts."""
    return dispatch(_context(), "search_tags", {"query": query, "limit": limit})


@mcp.tool()
def search_tracks(
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
) -> dict:
    """Candidate tracks matching tag/artist/year/rating/play-count/duration filters.
    any_tags filters; boost_tags (e.g. moods) only rank matches higher."""
    return dispatch(_context(), "search_tracks", locals())


@mcp.tool()
def list_genres(source: str = "any", top: int = 80) -> dict:
    """Tags with track counts; source = any | plex | lastfm | musicbrainz."""
    return dispatch(_context(), "list_genres", {"source": source, "top": top})


@mcp.tool()
def list_artists(
    tags: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict:
    """Artists in the library (most tracks first), filtered by tags/years/name."""
    return dispatch(_context(), "list_artists", locals())


@mcp.tool()
def get_artist_tracks(artist: str, sort: str = "rating", limit: int = 50) -> dict:
    """Tracks by one artist."""
    return dispatch(_context(), "get_artist_tracks", {"artist": artist, "sort": sort, "limit": limit})


@mcp.tool()
def sample_tracks(
    n: int = 30,
    any_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_user_rating: float | None = None,
) -> dict:
    """Random sample of tracks matching the filters - for variety beyond top results."""
    return dispatch(_context(), "sample_tracks", {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def get_similar_artists(artist: str, in_library_only: bool = True) -> dict:
    """Similar artists (Last.fm), by default only those the user owns."""
    return dispatch(
        _context(), "get_similar_artists", {"artist": artist, "in_library_only": in_library_only}
    )


@mcp.tool()
def get_track(plex_key: str) -> dict:
    """Full detail for one track, including every tag."""
    return dispatch(_context(), "get_track", {"plex_key": plex_key})


@mcp.tool()
def build_playlist_preview(plex_keys: list[str]) -> dict:
    """Runtime, artist/decade balance, duplicates and over-representation for a candidate list."""
    return dispatch(_context(), "build_playlist_preview", {"plex_keys": plex_keys})


@mcp.tool()
def create_plex_playlist(title: str, plex_keys: list[str], description: str | None = None) -> dict:
    """Create a new Plex playlist from an ordered list of plex_keys. Get user approval first."""
    return dispatch(
        _context(),
        "create_plex_playlist",
        {"title": title, "plex_keys": plex_keys, "description": description},
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
