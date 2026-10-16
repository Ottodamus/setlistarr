"""OpenAI and OpenAI-compatible endpoints (LM Studio, vLLM, llama.cpp server, Ollama's /v1)
via the official `openai` SDK's Chat Completions API."""

from __future__ import annotations

import json
import uuid
from typing import Any

import openai

from .base import ChatResponse, ProviderError, ToolCall, ToolSpec, Usage, parse_arguments


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
        http_client: Any = None,
    ):
        self.model = model
        opts = options or {}
        self.temperature = opts.get("temperature")
        self.max_output_tokens = opts.get("max_output_tokens")
        kwargs: dict[str, Any] = {
            # Local servers usually ignore the key, but the SDK insists on one.
            "api_key": api_key or "not-needed",
            "timeout": float(opts.get("timeout_sec", 600)),
            "max_retries": 2,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = openai.OpenAI(**kwargs)
        self._format_mode = "json_schema"  # degrades to json_object, then prompt-only

    @staticmethod
    def _convert(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            if role == "user":
                out.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in m["tool_calls"]
                    ]
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> ChatResponse:
        params: dict[str, Any] = {"model": self.model, "messages": self._convert(system, messages)}
        if tools:
            params["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if self.temperature is not None:
            params["temperature"] = float(self.temperature)
        if self.max_output_tokens:
            params["max_completion_tokens"] = int(self.max_output_tokens)

        while True:
            if json_schema and self._format_mode == "json_schema":
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "result", "schema": json_schema},
                }
            elif json_schema and self._format_mode == "json_object":
                params["response_format"] = {"type": "json_object"}
            else:
                params.pop("response_format", None)
            try:
                resp = self.client.chat.completions.create(**params)
                break
            except openai.BadRequestError as e:
                if json_schema and self._format_mode != "none" and "response_format" in str(e).lower():
                    self._format_mode = "json_object" if self._format_mode == "json_schema" else "none"
                    continue
                raise ProviderError(f"{self._label()} rejected the request: {_msg(e)}") from e
            except openai.APIError as e:
                raise self._map_error(e) from e

        if not resp.choices:
            raise ProviderError(f"{self._label()} returned no choices")
        choice = resp.choices[0]
        msg = choice.message
        calls = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            args, err = parse_arguments(fn.arguments)
            calls.append(ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:12]}", name=fn.name,
                                  arguments=args, parse_error=err))

        u = resp.usage
        cached = 0
        if u is not None and getattr(u, "prompt_tokens_details", None) is not None:
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
        prompt = (u.prompt_tokens or 0) if u else 0
        return ChatResponse(
            text=(msg.content or "").strip(),
            tool_calls=calls,
            usage=Usage(
                input_tokens=max(0, prompt - cached),  # OpenAI's prompt_tokens includes cached ones
                output_tokens=(u.completion_tokens or 0) if u else 0,
                cache_read_tokens=cached,
            ),
            stop_reason=choice.finish_reason,
            model=getattr(resp, "model", None) or self.model,
        )

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self.client.models.list())
        except openai.APIError as e:
            raise self._map_error(e) from e

    def _label(self) -> str:
        host = str(self.client.base_url)
        return "OpenAI" if "api.openai.com" in host else f"Endpoint {host}"

    def _map_error(self, e: openai.APIError) -> ProviderError:
        label = self._label()
        if isinstance(e, openai.AuthenticationError):
            return ProviderError(f"{label} rejected the API key")
        if isinstance(e, openai.NotFoundError):
            return ProviderError(f"{label}: not found (check the model name / base URL) - {_msg(e)}")
        if isinstance(e, openai.RateLimitError):
            return ProviderError(f"{label} rate limit hit - try again shortly", retryable=True)
        if isinstance(e, openai.APITimeoutError):
            return ProviderError(f"{label} timed out", retryable=True)
        if isinstance(e, openai.APIConnectionError):
            return ProviderError(f"Can't reach {label}: {_msg(e)}", retryable=True)
        if isinstance(e, openai.APIStatusError) and e.status_code >= 500:
            return ProviderError(f"{label} server error ({e.status_code})", retryable=True)
        return ProviderError(f"{label} error: {_msg(e)}")


def _msg(e: Exception) -> str:
    return getattr(e, "message", None) or str(e)
