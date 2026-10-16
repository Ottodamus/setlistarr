"""In-process APScheduler. The app must run with a single uvicorn worker, or every
worker would run its own copy of these jobs."""

from __future__ import annotations

import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings, bootstrap
from .core.db import connect
from .logs import get_logger
from .services.jobs import run_job
from .services.library import do_enrich, do_sync
from .services.recipes import run_recipe

log = get_logger(__name__)

def _plex_configured() -> bool:
    conn = connect(bootstrap().db_path)
    try:
        return Settings(conn).plex_configured()
    finally:
        conn.close()


def _sync(kind: str, full: bool):
    def job(trigger: str):
        # Before first-run setup, don't fill the job log with hourly "not configured" errors.
        if trigger == "schedule" and not _plex_configured():
            return None
        return run_job(kind, trigger, lambda c: do_sync(c, full=full))

    return job


JOBS = {
    "sync": _sync("sync", full=False),
    "sync-full": _sync("sync-full", full=True),
    "enrich": lambda trigger: run_job("enrich", trigger, do_enrich),
}


class Scheduler:
    def __init__(self) -> None:
        self._sched = BackgroundScheduler(
            timezone=os.environ.get("TZ") or None,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )

    def start(self) -> None:
        self._sched.start()
        self.reschedule()

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return self._sched.running

    def reschedule(self) -> None:
        """(Re)build the periodic jobs from current settings. Call after settings change."""
        conn = connect(bootstrap().db_path)
        try:
            s = Settings(conn)
            interval = s.get("sync_interval_minutes")
            cron = (s.get("full_sync_cron") or "").strip()
            recipes = [dict(r) for r in conn.execute(
                "SELECT id, name, schedule_cron FROM recipes WHERE enabled = 1 AND schedule_cron IS NOT NULL "
                "AND schedule_cron != ''")]
        finally:
            conn.close()

        for job in self._sched.get_jobs():
            if job.id.startswith("recipe-"):
                job.remove()
        for r in recipes:
            try:
                self._sched.add_job(
                    run_recipe, CronTrigger.from_crontab(r["schedule_cron"], timezone=self._sched.timezone),
                    args=[r["id"], "schedule"], id=f"recipe-{r['id']}", name=r["name"],
                )
            except ValueError as e:
                log.warning("scheduler.bad_recipe_cron", recipe_id=r["id"], cron=r["schedule_cron"], error=str(e))

        for job_id in ("periodic-sync", "periodic-sync-full"):
            if self._sched.get_job(job_id):
                self._sched.remove_job(job_id)
        if interval and interval > 0:
            self._sched.add_job(
                JOBS["sync"], IntervalTrigger(minutes=interval), args=["schedule"], id="periodic-sync"
            )
        if cron:
            try:
                self._sched.add_job(
                    JOBS["sync-full"],
                    CronTrigger.from_crontab(cron, timezone=self._sched.timezone),
                    args=["schedule"],
                    id="periodic-sync-full",
                )
            except ValueError as e:
                log.warning("scheduler.bad_cron", cron=cron, error=str(e))
        log.info("scheduler.rescheduled", sync_interval_minutes=interval, full_sync_cron=cron, recipes=len(recipes))

    def trigger(self, kind: str) -> None:
        """Run a job now on the scheduler's thread pool."""
        self._sched.add_job(JOBS[kind], args=["manual"], next_run_time=datetime.now(self._sched.timezone))

    def trigger_recipe(self, recipe_id: int) -> None:
        self._sched.add_job(run_recipe, args=[recipe_id, "manual"], next_run_time=datetime.now(self._sched.timezone))

    def recipe_next_runs(self) -> dict[int, datetime | None]:
        # Jobs only get next_run_time once the scheduler is running.
        return {int(j.id.split("-", 1)[1]): getattr(j, "next_run_time", None)
                for j in self._sched.get_jobs() if j.id.startswith("recipe-")}

    def next_runs(self) -> dict[str, datetime | None]:
        return {
            job.id: getattr(job, "next_run_time", None)
            for job in self._sched.get_jobs()
            if job.id.startswith("periodic-")
        }
