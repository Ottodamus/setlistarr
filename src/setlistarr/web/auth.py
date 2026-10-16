"""Single-admin auth.

* Passwords: stdlib scrypt (n=2**14, r=8, p=1), stored as scrypt$n$r$p$salt$hash (base64).
* ADMIN_USERNAME + ADMIN_PASSWORD env pin the one account (re-applied at every startup);
  otherwise the first visit goes to /setup.
* Signed session cookie (Starlette SessionMiddleware). Secure flag only if
  SETLISTARR_COOKIE_SECURE=1 - on plain-HTTP LAN access a Secure cookie silently never sticks.
* CSRF: SameSite=Lax cookie + an Origin/Referer host check on every non-GET request.
* SETLISTARR_AUTH_DISABLED=1 turns it all off (e.g. behind an auth proxy).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/health", "/login", "/setup"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def auth_disabled() -> bool:
    return os.environ.get("SETLISTARR_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def has_user(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(conn: sqlite3.Connection, username: str, password: str, *, from_env: bool = False) -> None:
    username = username.strip()
    if not username or len(password) < 8:
        raise ValueError("Username required and password must be at least 8 characters")
    with conn:
        conn.execute("DELETE FROM users")  # exactly one admin
        conn.execute(
            "INSERT INTO users(username, password_hash, password_from_env, created_at) VALUES(?,?,?,?)",
            (username, hash_password(password), int(from_env), datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def ensure_env_admin(conn: sqlite3.Connection) -> None:
    user, pw = os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
    if not (user and pw):
        return
    row = conn.execute("SELECT username, password_hash FROM users LIMIT 1").fetchone()
    if row and row["username"] == user and verify_password(pw, row["password_hash"]):
        conn.execute("UPDATE users SET password_from_env = 1")
        conn.commit()
        return
    create_user(conn, user, pw, from_env=True)


def check_login(conn: sqlite3.Connection, username: str, password: str) -> bool:
    row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if not row:
        hash_password(password)  # same work either way: don't reveal which usernames exist
        return False
    return verify_password(password, row["password_hash"])


def change_password(conn: sqlite3.Connection, current: str, new: str) -> None:
    row = conn.execute("SELECT username, password_hash, password_from_env FROM users LIMIT 1").fetchone()
    if not row:
        raise ValueError("No admin user")
    if row["password_from_env"]:
        raise ValueError("The password is set by ADMIN_PASSWORD - change it there")
    if not verify_password(current, row["password_hash"]):
        raise ValueError("Current password is wrong")
    create_user(conn, row["username"], new)


def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True  # non-browser clients (curl, tests); SameSite=Lax already covers browsers
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlparse(source).netloc == host


async def auth_middleware(request: Request, call_next, *, conn_factory) -> Response:
    path = request.url.path
    if request.method not in SAFE_METHODS and not _same_origin(request):
        return PlainTextResponse("Cross-origin request blocked", status_code=403)
    if auth_disabled() or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    conn = conn_factory()
    try:
        users_exist = has_user(conn)
    finally:
        conn.close()
    if not users_exist:
        return _redirect(request, "/setup")
    if not request.session.get("user"):
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return _redirect(request, f"/login?next={quote(nxt)}")
    return await call_next(request)


def _redirect(request: Request, to: str) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": to})
    if request.url.path.startswith("/api/"):
        return PlainTextResponse("Authentication required", status_code=401)
    return RedirectResponse(to, status_code=303)


def safe_next(nxt: str | None) -> str:
    """Only allow local redirects after login."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"
