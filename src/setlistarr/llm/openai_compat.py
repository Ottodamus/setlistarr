"""OpenAI and OpenAI-compatible endpoints (LM Studio, vLLM, llama.cpp server, Ollama's /v1)
via the official `openai` SDK's Chat Completions API."""

from __future__ import annotations

import json
import time
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
        self.image_quality = opts.get("image_quality") or "medium"
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

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> tuple[bytes, Usage, str]:
        """Image API (v1/images/generations). GPT image models return base64 by default; DALL-E
        needs response_format. Returns (image bytes, usage, model)."""
        import base64

        params: dict[str, Any] = {"model": self.model, "prompt": prompt, "size": size, "n": 1}
        if self.model.lower().startswith("dall-e"):
            params["response_format"] = "b64_json"
        else:
            params["quality"] = self.image_quality
        # SDK auto-retries would fire 3 requests within a second on a 429 - useless for a quota
        # problem and too fast for a real rate limit. One paced retry instead.
        client = self.client.with_options(max_retries=0)
        try:
            try:
                resp = client.images.generate(**params)
            except openai.RateLimitError as e:
                if _is_quota(e):
                    raise
                time.sleep(_retry_after(e, default=5.0, cap=20.0))
                resp = client.images.generate(**params)
        except openai.RateLimitError as e:
            raise self._map_error(e) from e
        except openai.BadRequestError as e:
            msg = _msg(e)
            if "safety" in msg.lower() or "moderation" in msg.lower() or "content_policy" in msg.lower():
                raise ProviderError(f"{self._label()} refused this cover prompt (content policy). Try rewording it.") from e
            raise ProviderError(f"{self._label()} rejected the image request: {msg}") from e
        except openai.APIError as e:
            raise self._map_error(e) from e
        item = (resp.data or [None])[0]
        if item is None or not getattr(item, "b64_json", None):
            raise ProviderError(f"{self._label()} returned no image")
        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0 if u else 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0 if u else 0,
        )
        return base64.b64decode(item.b64_json), usage, self.model

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
            if _is_quota(e):
                return ProviderError(
                    f"{label} says this account has no credit left (insufficient_quota). Add a payment method "
                    "or prepaid credit at platform.openai.com/settings/organization/billing - API billing is "
                    "separate from a ChatGPT subscription.")
            wait = _retry_after(e, default=0, cap=3600)
            return ProviderError(
                f"{label} rate limit hit: {_msg(e)}" + (f" - try again in ~{wait:.0f}s" if wait else ""),
                retryable=True)
        if isinstance(e, openai.APITimeoutError):
            return ProviderError(f"{label} timed out", retryable=True)
        if isinstance(e, openai.APIConnectionError):
            return ProviderError(f"Can't reach {label}: {_msg(e)}", retryable=True)
        if isinstance(e, openai.APIStatusError) and e.status_code >= 500:
            return ProviderError(f"{label} server error ({e.status_code})", retryable=True)
        return ProviderError(f"{label} error: {_msg(e)}")


def _is_quota(e: Exception) -> bool:
    """OpenAI sends 'out of credit' as a 429 too; tell it apart from real rate limiting."""
    code = (getattr(e, "code", None) or "").lower()
    return code == "insufficient_quota" or "insufficient_quota" in str(e).lower() or "exceeded your current quota" in str(e).lower()


def _retry_after(e: Exception, *, default: float, cap: float) -> float:
    resp = getattr(e, "response", None)
    raw = resp.headers.get("retry-after") if resp is not None else None
    try:
        return min(float(raw), cap) if raw else default
    except ValueError:
        return default


def _msg(e: Exception) -> str:
    """OpenAI's own error text, not the SDK's 'Error code: 429 - {...}' wrapper."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if inner.get("message"):
            return str(inner["message"])
    return getattr(e, "message", None) or str(e)
