"""State shared by both generation modes: usage/cost metering, progress events, cancellation."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import ChatResponse, Provider, Usage
from ..llm.registry import ProviderConfig, cost_usd

Emit = Callable[..., None]  # emit(kind, message, **data)


class Cancelled(Exception):
    pass


class ToolModeUnusable(Exception):
    """The model can't drive tool calling well enough; auto mode falls back to two-stage."""


class Meter:
    def __init__(self, conn: sqlite3.Connection, cfg: ProviderConfig):
        self.conn, self.cfg = conn, cfg
        self.usage = Usage()
        self.calls = 0
        self.cost = 0.0
        self.cost_known = True

    def record(self, resp: ChatResponse) -> None:
        self.usage.add(resp.usage)
        self.calls += 1
        c = cost_usd(self.conn, self.cfg, resp.model or self.cfg.model, resp.usage)
        if c is None:
            self.cost_known = False
        else:
            self.cost += c

    @property
    def context_tokens(self) -> int:
        u = self.usage
        return u.input_tokens + u.cache_read_tokens + u.cache_write_tokens

    def summary(self) -> dict[str, Any]:
        u = self.usage
        return {
            "calls": self.calls, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
            "cost_usd": round(self.cost, 5) if self.cost_known else None,
        }


@dataclass
class GenContext:
    conn: sqlite3.Connection
    provider: Provider
    cfg: ProviderConfig
    meter: Meter
    emit: Emit
    cancel: threading.Event = field(default_factory=threading.Event)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def chat(self, **kwargs) -> ChatResponse:
        self.check_cancel()
        resp = self.provider.chat(**kwargs)
        self.meter.record(resp)
        return resp


@dataclass
class ModeResult:
    mode: str
    picks: list[str]
    pool: list[str]
    title: str | None = None
    reasoning: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # mode-level adjustments (relaxed filters, etc.)
