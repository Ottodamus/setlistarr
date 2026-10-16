"""Provider-neutral chat types.

Conversation history is a list of plain dicts in one neutral shape:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...], "raw": <provider data>}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}

Adapters translate to their wire format. "raw" carries the provider's own assistant
payload (e.g. Anthropic content blocks incl. thinking) so the same adapter can echo it back
verbatim; history is append-only and stays on one provider for its lifetime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (type: object)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None  # set when the model sent arguments that aren't a JSON object


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class ChatResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    raw: Any = None
    model: str | None = None  # the model that actually served the call (fallbacks can change it)

    def as_message(self) -> dict[str, Any]:
        return {"role": "assistant", "content": self.text, "tool_calls": self.tool_calls, "raw": self.raw}


class ProviderError(Exception):
    """An LLM failure with a message fit for the UI. retryable=False means don't bother."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    kind: str
    model: str

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> ChatResponse: ...

    def list_models(self) -> list[str]: ...


# --------------------------------------------------------------------------- #
# helpers shared by adapters
# --------------------------------------------------------------------------- #
def parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Tool-call arguments arrive as a dict (Anthropic, Ollama) or a JSON string (OpenAI)."""
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        val = json.loads(raw)
    except (TypeError, ValueError) as e:
        return {}, f"arguments were not valid JSON: {e}"
    if not isinstance(val, dict):
        return {}, "arguments must be a JSON object"
    return val, None


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Best-effort JSON from model text: whole string, fenced block, or first {...} span."""
    text = (text or "").strip()
    candidates = [text]
    candidates += _FENCE.findall(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (TypeError, ValueError):
            continue
    raise ValueError("no JSON object found in model output")
