"""Native Ollama adapter (/api/chat) over httpx.

Why not only the OpenAI-compatible /v1 endpoint: native lets us set num_ctx per request
(Ollama's default context window silently truncates long tool loops) and pass a JSON schema
as `format`, which constrains decoding - the main reason two-stage mode works on small models.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .base import ChatResponse, ProviderError, ToolCall, ToolSpec, Usage, parse_arguments


class OllamaProvider:
    kind = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
        **_: Any,
    ):
        self.model = model
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")
        opts = options or {}
        self.num_ctx = int(opts.get("num_ctx", 16384))
        # Cap output per call: small models can loop until num_predict is exhausted, which at
        # local speeds means many minutes per call.
        self.max_output_tokens = int(opts.get("max_output_tokens", 2048))
        self.temperature = opts.get("temperature")
        self.keep_alive = opts.get("keep_alive", "10m")
        self.think = opts.get("think")  # None = model default; False disables for thinking models
        self._http = httpx.Client(
            base_url=self.base_url, timeout=float(opts.get("timeout_sec", 300)), transport=transport
        )

    @staticmethod
    def _convert(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            if role == "user":
                out.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"function": {"name": c.name, "arguments": c.arguments}} for c in m["tool_calls"]
                    ]
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_name": m.get("name", ""), "content": m["content"]})
        return out

    def _post(self, path: str, body: dict[str, Any] | None = None, method: str = "POST") -> dict:
        try:
            resp = self._http.request(method, path, json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(f"Ollama at {self.base_url} timed out", retryable=True) from e
        except httpx.TransportError as e:
            raise ProviderError(f"Can't reach Ollama at {self.base_url}: {e}", retryable=True) from e
        if resp.status_code == 404:
            raise ProviderError(
                f"Ollama doesn't have model '{self.model}' - run `ollama pull {self.model}`"
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                detail = resp.json().get("error", detail)
            except ValueError:
                pass
            raise ProviderError(f"Ollama error ({resp.status_code}): {detail}",
                                retryable=resp.status_code >= 500)
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError("Ollama sent a non-JSON response") from e

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> ChatResponse:
        options: dict[str, Any] = {"num_ctx": self.num_ctx, "num_predict": min(max_tokens, self.max_output_tokens)}
        if self.temperature is not None:
            options["temperature"] = float(self.temperature)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert(system, messages),
            "stream": False,
            "options": options,
            "keep_alive": self.keep_alive,
        }
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if json_schema:
            body["format"] = json_schema
        if self.think is not None:
            body["think"] = bool(self.think)

        data = self._post("/api/chat", body)
        msg = data.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args, err = parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                  name=fn.get("name", ""), arguments=args, parse_error=err))
        return ChatResponse(
            text=(msg.get("content") or "").strip(),
            tool_calls=calls,
            usage=Usage(
                input_tokens=int(data.get("prompt_eval_count") or 0),
                output_tokens=int(data.get("eval_count") or 0),
            ),
            stop_reason=data.get("done_reason"),
            model=self.model,
        )

    def list_models(self) -> list[str]:
        return sorted(m["name"] for m in self._post("/api/tags", method="GET").get("models", []))

