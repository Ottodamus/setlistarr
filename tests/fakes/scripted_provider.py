"""A Provider that replays a script. Each step is a ChatResponse, or a callable taking the
request kwargs and returning one (so a step can react to earlier tool results)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from setlistarr.llm.base import ChatResponse, ToolCall, Usage

_ids = itertools.count(1)


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, arguments=arguments)


def tools(*calls: ToolCall, text: str = "") -> ChatResponse:
    return ChatResponse(text=text, tool_calls=list(calls), usage=Usage(1000, 100))


def say(text: str) -> ChatResponse:
    return ChatResponse(text=text, usage=Usage(500, 50))


class ScriptedProvider:
    kind = "scripted"

    def __init__(self, steps: list[ChatResponse | Callable[[dict], ChatResponse]], model: str = "claude-opus-5-5"):
        self.steps = list(steps)
        self.model = model
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> ChatResponse:
        # snapshot: the caller keeps appending to the same messages list
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self.steps:
            raise AssertionError("script exhausted")
        step = self.steps.pop(0)
        resp = step(kwargs) if callable(step) else step
        resp.model = resp.model or self.model
        return resp

    def list_models(self) -> list[str]:
        return [self.model]

    def last_tool_result(self, name: str) -> str | None:
        msgs = self.requests[-1]["messages"]
        for m in reversed(msgs):
            if m["role"] == "tool" and m["name"] == name:
                return m["content"]
        return None
