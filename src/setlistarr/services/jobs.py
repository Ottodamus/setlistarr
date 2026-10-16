"""Background job bookkeeping: every sync/enrich run gets a row in `jobs`.

Library-writing jobs (sync, enrich) share one lock, so a scheduled sync and a manual
"Sync now" can't interleave writes. A job that can't get the lock is skipped and recorded.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..config import bootstrap
from ..core.db import connect
from ..core.plex.client import PlexError
from ..logs import get_logger

log = get_logger(__name__)

_library_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def library_busy() -> bool:
    return _library_lock.locked()


def mark_interrupted(conn: sqlite3.Connection) -> None:
    """Jobs left 'running' by a previous process died with it."""
    conn.execute(
        "UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (_now(),)
    )
    conn.commit()


def run_job(
    kind: str, trigger: str, fn: Callable[[sqlite3.Connection], dict[str, Any]]
) -> dict[str, Any] | None:
    """Run fn(conn) under the library lock, recording the outcome. Never raises."""
    conn = connect(bootstrap().db_path)
    try:
        if not _library_lock.acquire(blocking=False):
            conn.execute(
                "INSERT INTO jobs(kind, trigger, started_at, finished_at, status, error) "
                "VALUES(?, ?, ?, ?, 'skipped', 'another library job was running')",
                (kind, trigger, _now(), _now()),
            )
            conn.commit()
            log.info("job.skipped_busy", kind=kind, trigger=trigger)
            return None
        try:
            job_id = conn.execute(
                "INSERT INTO jobs(kind, trigger, started_at, status) VALUES(?, ?, ?, 'running')",
                (kind, trigger, _now()),
            ).lastrowid
            conn.commit()
            log.info("job.start", job_id=job_id, kind=kind, trigger=trigger)
            status, stats, error = "ok", None, None
            try:
                stats = fn(conn)
            except PlexError as e:
                status, error = "error", str(e)
                log.warning("job.plex_error", job_id=job_id, kind=kind, error=error)
            except Exception as e:  # keep the scheduler alive; full trace goes to the log
                status, error = "error", f"{type(e).__name__}: {e}"
                log.exception("job.failed", job_id=job_id, kind=kind)
            conn.rollback()  # drop any half-done transaction from a failed fn
            conn.execute(
                "UPDATE jobs SET finished_at = ?, status = ?, stats_json = ?, error = ? WHERE id = ?",
                (_now(), status, json.dumps(stats, default=str) if stats else None, error, job_id),
            )
            conn.commit()
            log.info("job.finish", job_id=job_id, kind=kind, status=status)
            return stats
        finally:
            _library_lock.release()
    finally:
        conn.close()


def recent_jobs(conn: sqlite3.Connection, limit: int = 25) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = json.loads(d.pop("stats_json")) if d.get("stats_json") else None
        out.append(d)
    return out
