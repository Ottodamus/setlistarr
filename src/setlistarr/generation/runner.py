"""Run generations in background threads, stream progress, persist the result.

Flow: generations row (running) -> mode (tools | two_stage; auto = tools, falling back to
two_stage if the model can't drive tools) -> finalize() -> drafts/draft_tracks -> row (ok).
Progress events live in memory for SSE while running and are saved to trace_json at the end.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..config import Settings, bootstrap
from ..core.db import connect
from ..llm.base import ProviderError
from ..llm.registry import ProviderConfig, build_provider, get_provider
from ..logs import get_logger
from .common import Cancelled, GenContext, Meter, ModeResult, ToolModeUnusable
from .constraints import Constraints
from .tool_agent import run_tool_agent
from .two_stage import run_two_stage
from .validate import finalize

log = get_logger(__name__)
MAX_CONCURRENT = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.done = False
        self._cond = threading.Condition()
        self._t0 = time.monotonic()

    def emit(self, kind: str, message: str, **data: Any) -> None:
        with self._cond:
            self.events.append({"seq": len(self.events) + 1, "t": round(time.monotonic() - self._t0, 1),
                                "kind": kind, "msg": message, **data})
            self._cond.notify_all()

    def finish(self) -> None:
        with self._cond:
            self.done = True
            self._cond.notify_all()

    def wait_after(self, seq: int, timeout: float) -> tuple[list[dict[str, Any]], bool]:
        with self._cond:
            if len(self.events) <= seq and not self.done:
                self._cond.wait(timeout)
            return self.events[seq:], self.done


class GenerationManager:
    def __init__(self) -> None:
        self._logs: dict[int, EventLog] = {}
        self._cancel: dict[int, threading.Event] = {}
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def start(
        self,
        conn: sqlite3.Connection,
        prompt: str,
        *,
        provider_id: int | None = None,
        constraints: Constraints | None = None,
        mode: str | None = None,
        background: bool = True,
        recipe_id: int | None = None,
        seed_keys: list[str] | None = None,
        parent_draft_id: int | None = None,
        inject: dict[str, Any] | None = None,
    ) -> int:
        """seed_keys: user-kept tracks a regeneration must build around."""
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is empty")
        cfg = get_provider(conn, provider_id)
        if cfg is None:
            raise ProviderError("No AI provider configured - add one in Settings -> AI providers")
        if constraints is None:
            constraints = default_constraints(conn)
        mode = mode or cfg.tool_mode
        gen_id = conn.execute(
            "INSERT INTO generations(created_at, prompt, constraints_json, provider_id, provider_name, model, "
            "mode_requested, status, recipe_id, parent_draft_id) VALUES(?,?,?,?,?,?,?, 'running', ?, ?)",
            (_now(), prompt, json.dumps(constraints.to_dict()), cfg.id, cfg.name, cfg.model, mode, recipe_id,
             parent_draft_id),
        ).lastrowid
        conn.commit()

        events, cancel = EventLog(), threading.Event()
        with self._lock:
            self._logs[gen_id], self._cancel[gen_id] = events, cancel
        args = (gen_id, prompt, cfg, constraints, mode, events, cancel, inject or {}, [str(k) for k in seed_keys or []])
        if background:
            threading.Thread(target=self._run, args=args, name=f"gen-{gen_id}", daemon=True).start()
        else:
            self._run(*args)
        return gen_id

    def events(self, gen_id: int) -> EventLog | None:
        return self._logs.get(gen_id)

    def cancel(self, gen_id: int) -> bool:
        ev = self._cancel.get(gen_id)
        if ev:
            ev.set()
        return ev is not None

    # ------------------------------------------------------------------ #
    def _run(self, gen_id, prompt, cfg: ProviderConfig, constraints: Constraints, mode, events: EventLog,
             cancel: threading.Event, inject, seed: list[str]) -> None:
        conn = connect(bootstrap().db_path)
        t0 = time.monotonic()
        meter = Meter(conn, cfg)
        status, error, result, draft_id = "ok", None, None, None
        try:
            if not self._slots.acquire(timeout=0.1):
                events.emit("stage", f"waiting for a free slot ({MAX_CONCURRENT} generations at a time)")
                self._slots.acquire()
            try:
                events.emit("stage", f"{cfg.name} · {cfg.model} · mode {mode}"
                                     + (f" · keeping {len(seed)} track(s)" if seed else ""))
                provider = inject.get("provider") or build_provider(cfg, **inject)
                gx = GenContext(conn=conn, provider=provider, cfg=cfg, meter=meter, emit=events.emit, cancel=cancel)
                result = self._run_mode(gx, prompt, constraints, mode, seed)
                gx.check_cancel()
                draft_id = self._save(conn, gen_id, prompt, constraints, result, seed)
            finally:
                self._slots.release()
        except Cancelled:
            status, error = "cancelled", "Cancelled"
        except ProviderError as e:
            status, error = "error", str(e)
            log.warning("generation.provider_error", gen_id=gen_id, error=error)
        except Exception as e:
            status, error = "error", f"{type(e).__name__}: {e}"
            log.exception("generation.failed", gen_id=gen_id)

        if error:
            events.emit("error", error)
        else:
            events.emit("done", "draft ready", draft_id=draft_id)
        s = meter.summary()
        conn.execute(
            "UPDATE generations SET finished_at=?, status=?, error=?, mode_used=?, title=?, reasoning=?, "
            "input_tokens=?, output_tokens=?, cache_read_tokens=?, cache_write_tokens=?, cost_usd=?, "
            "llm_calls=?, duration_ms=?, trace_json=? WHERE id=?",
            (_now(), status, error, result.mode if result else None,
             result.title if result else None, result.reasoning if result else None,
             s["input_tokens"], s["output_tokens"], s["cache_read_tokens"], s["cache_write_tokens"],
             s["cost_usd"], s["calls"], int((time.monotonic() - t0) * 1000),
             json.dumps(events.events, default=str), gen_id),
        )
        conn.commit()
        conn.close()
        log.info("generation.finish", gen_id=gen_id, status=status, mode=result.mode if result else None, **s)
        events.finish()
        with self._lock:
            self._cancel.pop(gen_id, None)
        # keep the event log briefly so a late SSE client still gets the end
        reaper = threading.Timer(300, lambda: self._logs.pop(gen_id, None))
        reaper.daemon = True
        reaper.start()

    @staticmethod
    def _run_mode(gx: GenContext, prompt: str, c: Constraints, mode: str, seed: list[str]) -> ModeResult:
        if mode == "two_stage":
            return run_two_stage(gx, prompt, c, seed)
        try:
            return run_tool_agent(gx, prompt, c, seed)
        except ToolModeUnusable as e:
            if mode == "tools":
                raise ProviderError(f"Tool-calling mode failed: {e}") from e
            gx.emit("warn", f"tool-calling didn't work ({e}) - switching to two-stage mode")
            result = run_two_stage(gx, prompt, c, seed)
            result.notes.insert(0, f"Fell back to two-stage mode: {e}")
            return result

    @staticmethod
    def _save(conn, gen_id: int, prompt: str, c: Constraints, r: ModeResult, seed: list[str]) -> int:
        keep = set(seed)
        final = finalize(conn, r.picks, c, r.pool, sources=r.sources, keep=keep)
        if not final.tracks:
            raise ProviderError("No valid tracks were selected")
        title = r.title or prompt[:60]
        now = _now()
        with conn:
            draft_id = conn.execute(
                "INSERT INTO drafts(generation_id, title, description, adjustments_json, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (gen_id, title, r.reasoning, json.dumps(r.notes + final.adjustments), now, now),
            ).lastrowid
            conn.executemany(
                "INSERT INTO draft_tracks(draft_id, position, plex_key, source, locked) VALUES(?,?,?,?,?)",
                [(draft_id, i, t["plex_key"], t.get("source", "model"), int(t["plex_key"] in keep))
                 for i, t in enumerate(final.tracks)],
            )
            conn.execute("UPDATE generations SET constraints_json = ? WHERE id = ?",
                         (json.dumps(c.to_dict()), gen_id))
        return draft_id


def default_constraints(conn: sqlite3.Connection) -> Constraints:
    s = Settings(conn)
    return Constraints(default_target_minutes=s.get("default_target_minutes"),
                       default_max_per_artist=s.get("default_max_per_artist"))


def mark_interrupted(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE generations SET status = 'interrupted', finished_at = ?, error = 'App restarted mid-run' "
        "WHERE status = 'running'", (_now(),),
    )
    conn.commit()


manager = GenerationManager()
