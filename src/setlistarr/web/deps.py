"""Request-scoped dependencies and the shared Jinja environment."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import bootstrap
from ..core.db import connect

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(bootstrap().db_path)
    try:
        yield conn
    finally:
        conn.close()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --------------------------------------------------------------------------- #
# template filters
# --------------------------------------------------------------------------- #
def _duration(ms) -> str:
    if not ms:
        return "-"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _hours(ms) -> str:
    if not ms:
        return "0 h"
    h = int(ms) / 3_600_000
    return f"{h / 24:.1f} days" if h >= 48 else f"{h:.1f} h"


def _stars(rating) -> str:
    if rating is None:
        return ""
    full = round(float(rating) / 2)
    return "★" * full + "☆" * (5 - full)


def _ago(iso) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "in the future"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit} ago"
    return f"{secs}s ago"


def _num(n) -> str:
    return f"{int(n or 0):,}"


templates.env.filters.update(duration=_duration, hours=_hours, stars=_stars, ago=_ago, num=_num)
templates.env.globals.update(version=__version__)
