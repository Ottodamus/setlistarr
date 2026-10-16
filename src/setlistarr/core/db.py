"""SQLite helpers: connection, migrations, small query utilities.

Migrations are numbered files in core/migrations/ (NNNN_name.sql), applied in order
and tracked with PRAGMA user_version. Never edit a migration that has shipped; add a new one.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d{4})_[\w-]+\.sql$")


def connect(db_path: Path | str) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may run a request's dependency and endpoint on
    # different threadpool threads. Each connection is still used by one request at a time.
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def restrict_permissions(db_path: Path | str) -> None:
    """The DB holds the Plex token and API keys: owner-only, including WAL/SHM side files."""
    base = Path(db_path)
    for p in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
        try:
            if p.exists():
                p.chmod(0o600)
        except OSError:
            pass  # e.g. a read-only or foreign-owned mount; not worth failing startup over


def _migrations() -> list[tuple[int, str, str]]:
    out = []
    for entry in resources.files("setlistarr.core").joinpath("migrations").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text()))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, _name, sql in _migrations():
        if version <= current:
            continue
        # executescript commits any open transaction first; wrap each file so a
        # failing migration leaves the schema at the previous version.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        current = version
    return current


def latest_version() -> int:
    migs = _migrations()
    return migs[-1][0] if migs else 0


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
