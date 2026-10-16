"""Provider configs (the `providers` table) -> live Provider instances, plus pricing."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .base import Provider, ProviderError, Usage
from .claude import ClaudeProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider

KINDS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "base_url": "",
        "model": "claude-opus-5-5",
        "key_env": "ANTHROPIC_API_KEY",
        "tool_mode": "auto",
        "options": {"effort": "medium", "fallbacks": True, "max_steps": 30, "tool_patience": 20,
                    "token_budget": 400_000},
    },
    "openai_compat": {
        "label": "OpenAI / OpenAI-compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "key_env": "OPENAI_API_KEY",
        "tool_mode": "auto",
        "options": {"temperature": None, "max_steps": 30, "tool_patience": 20, "token_budget": 400_000},
    },
    "ollama": {
        "label": "Ollama (native)",
        "base_url": "http://localhost:11434",
        "model": "qwen3:14b",
        "key_env": None,
        # Small local models drive tool loops poorly and slowly; two-stage is 2 calls.
        "tool_mode": "two_stage",
        "options": {"num_ctx": 16384, "temperature": 0.6, "keep_alive": "10m", "max_output_tokens": 2048,
                    "max_steps": 25, "tool_patience": 8, "token_budget": 0},
    },
}
LOCAL_KINDS = {"ollama"}


@dataclass
class ProviderConfig:
    id: int | None
    name: str
    kind: str
    model: str
    base_url: str = ""
    api_key: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    tool_mode: str = "auto"
    is_default: bool = False

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "ProviderConfig":
        return cls(
            id=r["id"], name=r["name"], kind=r["kind"], model=r["model"], base_url=r["base_url"] or "",
            api_key=r["api_key"] or "", options=json.loads(r["options_json"] or "{}"),
            tool_mode=r["tool_mode"], is_default=bool(r["is_default"]),
        )

    def option(self, key: str) -> Any:
        if key in self.options and self.options[key] not in (None, ""):
            return self.options[key]
        return KINDS[self.kind]["options"].get(key)

    def effective_options(self) -> dict[str, Any]:
        return {**KINDS[self.kind]["options"], **{k: v for k, v in self.options.items() if v not in (None, "")}}

    @property
    def is_local(self) -> bool:
        if self.kind in LOCAL_KINDS:
            return True
        host = self.base_url.lower()
        return self.kind == "openai_compat" and bool(host) and "api.openai.com" not in host

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        env = KINDS[self.kind]["key_env"]
        return os.environ.get(env, "").strip() or None if env else None


def build_provider(cfg: ProviderConfig, **inject: Any) -> Provider:
    """inject: test hooks (http_client for SDK adapters, transport for Ollama)."""
    opts = cfg.effective_options()
    if cfg.kind == "anthropic":
        return ClaudeProvider(cfg.model, api_key=cfg.resolved_api_key(), base_url=cfg.base_url or None,
                              options=opts, http_client=inject.get("http_client"))
    if cfg.kind == "openai_compat":
        return OpenAICompatProvider(cfg.model, api_key=cfg.resolved_api_key(),
                                    base_url=cfg.base_url or None, options=opts,
                                    http_client=inject.get("http_client"))
    if cfg.kind == "ollama":
        return OllamaProvider(cfg.model, base_url=cfg.base_url or None, options=opts,
                              transport=inject.get("transport"))
    raise ProviderError(f"unknown provider kind {cfg.kind!r}")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def list_providers(conn: sqlite3.Connection) -> list[ProviderConfig]:
    return [ProviderConfig.from_row(r) for r in conn.execute("SELECT * FROM providers ORDER BY name")]


def get_provider(conn: sqlite3.Connection, provider_id: int | None = None) -> ProviderConfig | None:
    """By id, else the default, else the only/first configured provider."""
    if provider_id:
        row = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
        return ProviderConfig.from_row(row) if row else None
    row = conn.execute("SELECT * FROM providers ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return ProviderConfig.from_row(row) if row else None


def get_provider_by_name(conn: sqlite3.Connection, name: str) -> ProviderConfig | None:
    row = conn.execute("SELECT * FROM providers WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
    return ProviderConfig.from_row(row) if row else None


def save_provider(conn: sqlite3.Connection, cfg: ProviderConfig) -> int:
    if cfg.kind not in KINDS:
        raise ValueError(f"unknown kind {cfg.kind}")
    if cfg.tool_mode not in ("auto", "tools", "two_stage"):
        raise ValueError(f"bad tool_mode {cfg.tool_mode}")
    opts = json.dumps({k: v for k, v in cfg.options.items() if v not in (None, "")}, sort_keys=True)
    with conn:
        if cfg.id:
            conn.execute(
                "UPDATE providers SET name=?, kind=?, base_url=?, api_key=?, model=?, options_json=?, "
                "tool_mode=? WHERE id=?",
                (cfg.name, cfg.kind, cfg.base_url, cfg.api_key, cfg.model, opts, cfg.tool_mode, cfg.id),
            )
            pid = cfg.id
        else:
            pid = conn.execute(
                "INSERT INTO providers(name, kind, base_url, api_key, model, options_json, tool_mode, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (cfg.name, cfg.kind, cfg.base_url, cfg.api_key, cfg.model, opts, cfg.tool_mode,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ).lastrowid
        if cfg.is_default or conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1:
            conn.execute("UPDATE providers SET is_default = (id = ?)", (pid,))
    return pid


def delete_provider(conn: sqlite3.Connection, provider_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        if not conn.execute("SELECT 1 FROM providers WHERE is_default = 1").fetchone():
            conn.execute("UPDATE providers SET is_default = 1 WHERE id = (SELECT MIN(id) FROM providers)")


def set_default(conn: sqlite3.Connection, provider_id: int) -> None:
    with conn:
        conn.execute("UPDATE providers SET is_default = (id = ?)", (provider_id,))


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
def cost_hint(conn: sqlite3.Connection, cfg: ProviderConfig) -> str:
    """One line for the UI: typical cost per run for this provider."""
    if cfg.is_local:
        return "local model - free"
    row = conn.execute(
        "SELECT AVG(cost_usd) AS avg, COUNT(*) AS n FROM (SELECT cost_usd FROM generations "
        "WHERE provider_id = ? AND model = ? AND status = 'ok' AND cost_usd IS NOT NULL "
        "ORDER BY id DESC LIMIT 10)",
        (cfg.id, cfg.model),
    ).fetchone()
    price = conn.execute("SELECT * FROM model_pricing WHERE model = ?", (cfg.model,)).fetchone()
    per_mtok = f"${price['input_per_mtok']:g} in / ${price['output_per_mtok']:g} out per M tokens" if price else None
    if row["n"]:
        return f"~${row['avg']:.3f} per run (avg of last {row['n']})" + (f" · {per_mtok}" if per_mtok else "")
    return per_mtok or f"no pricing for {cfg.model} - add it under AI providers to track cost"



def cost_usd(conn: sqlite3.Connection, cfg: ProviderConfig, model: str, usage: Usage) -> float | None:
    """USD for one call. 0 for local providers; None when a paid model has no pricing row."""
    if cfg.is_local:
        return 0.0
    row = conn.execute("SELECT * FROM model_pricing WHERE model = ?", (model,)).fetchone()
    if row is None:
        return None
    inp = row["input_per_mtok"]
    read = row["cache_read_per_mtok"] if row["cache_read_per_mtok"] is not None else inp * 0.1
    write = row["cache_write_per_mtok"] if row["cache_write_per_mtok"] is not None else inp * 1.25
    return (
        usage.input_tokens * inp
        + usage.output_tokens * row["output_per_mtok"]
        + usage.cache_read_tokens * read
        + usage.cache_write_tokens * write
    ) / 1_000_000
