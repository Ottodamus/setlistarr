"""FastAPI application factory."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config import bootstrap
from ..core.db import connect, migrate, restrict_permissions
from ..core.plex.client import PlexError
from ..generation.runner import mark_interrupted as mark_generations_interrupted
from ..logs import get_logger, setup_logging
from ..scheduler import Scheduler
from ..services.jobs import mark_interrupted
from .deps import is_htmx, templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .routes import auth as auth_routes
from .routes import covers, discover, drafts, generate, health, history, home, library, providers, recipes, settings, stats, system

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    b = bootstrap()
    conn = connect(b.db_path)
    try:
        version = migrate(conn)
        mark_interrupted(conn)
        mark_generations_interrupted(conn)
        auth.ensure_env_admin(conn)
    finally:
        conn.close()
    restrict_permissions(b.db_path)
    log.info("startup", db=str(b.db_path), schema_version=version)

    app.state.scheduler = Scheduler()
    if os.environ.get("SETLISTARR_DISABLE_SCHEDULER") != "1":
        app.state.scheduler.start()
    try:
        yield
    finally:
        app.state.scheduler.shutdown()


def create_app() -> FastAPI:
    b = bootstrap()
    setup_logging(b.log_level, b.log_format)
    app = FastAPI(title="Setlistarr", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    for module in (home, auth_routes, health, library, stats, settings, providers, system, generate, drafts,
                   history, recipes, covers, discover):
        app.include_router(module.router)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        return await auth.auth_middleware(request, call_next, conn_factory=lambda: connect(b.db_path))

    # Added last = outermost, so the session is decoded before require_login runs.
    app.add_middleware(
        SessionMiddleware, secret_key=b.secret_key, session_cookie="setlistarr_session",
        max_age=30 * 24 * 3600, same_site="lax", https_only=b.cookie_secure,
    )

    @app.exception_handler(PlexError)
    async def plex_error(request: Request, exc: PlexError):
        log.warning("plex_error", path=request.url.path, error=str(exc))
        return _error(request, 502, "Plex error", str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return _error(request, exc.status_code, "Error", str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled_error", path=request.url.path)
        return _error(request, 500, "Something broke", f"{type(exc).__name__}: {exc}")

    return app


def _error(request: Request, status: int, title: str, message: str):
    if request.url.path.startswith("/api/") or request.url.path == "/health":
        return JSONResponse({"error": message}, status_code=status)
    if is_htmx(request):
        return HTMLResponse(f'<div class="alert alert-error">{_esc(message)}</div>', status_code=200)
    return templates.TemplateResponse(
        request, "error.html", {"title": title, "message": message, "status": status}, status_code=status
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
