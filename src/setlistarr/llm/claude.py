"""Anthropic (Claude) adapter - official `anthropic` SDK.

Choices, per the Claude API docs:
  * Assistant turns are echoed back verbatim (response.content, incl. thinking blocks), and
    history is append-only, which preserved thinking requires.
  * Prompt caching: explicit breakpoint on the static system prompt + top-level automatic
    caching for the growing tool loop. Keep the system prompt byte-stable.
  * Server-side refusal fallback (`fallbacks="default"`) is on by default; on models without
    fallback support the request just runs on the requested model.
  * effort is passed via output_config (Opus 5.5 defaults to "medium" if unset).
  * Forced tool_choice is rejected on current models - we always use auto + prompting.
"""

from __future__ import annotations

from typing import Any

import anthropic

from .base import ChatResponse, ProviderError, ToolCall, ToolSpec, Usage, parse_arguments

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    kind = "anthropic"

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
        self.effort = opts.get("effort") or None
        self.fallbacks = opts.get("fallbacks", True)
        kwargs: dict[str, Any] = {"timeout": float(opts.get("timeout_sec", 600)), "max_retries": 2}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        try:
            self.client = anthropic.Anthropic(**kwargs)
        except anthropic.AnthropicError as e:  # e.g. no credentials anywhere
            raise ProviderError(f"Anthropic client setup failed: {e}") from e
        self._structured_ok = True

    # ------------------------------------------------------------------ #
    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "user":
                out.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                if m.get("raw") is not None:
                    content = m["raw"]
                else:
                    content = []
                    if m.get("content"):
                        content.append({"type": "text", "text": m["content"]})
                    for c in m.get("tool_calls") or []:
                        content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
                out.append({"role": "assistant", "content": content})
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                    "is_error": bool(m.get("is_error")),
                }
                # All results for one assistant turn go back in a single user message.
                prev = out[-1] if out else None
                if (
                    prev
                    and prev["role"] == "user"
                    and isinstance(prev["content"], list)
                    and all(b.get("type") == "tool_result" for b in prev["content"])
                ):
                    prev["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
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
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._convert(messages),
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        output_config: dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if json_schema and self._structured_ok:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            params["output_config"] = output_config

        try:
            resp = self._create(params)
        except anthropic.BadRequestError as e:
            if "format" in output_config and _mentions(e, "output_config", "format", "schema"):
                # Schema not accepted (or structured outputs unsupported): fall back to
                # prompt-only JSON; the caller parses leniently either way.
                self._structured_ok = False
                output_config.pop("format")
                if output_config:
                    params["output_config"] = output_config
                else:
                    params.pop("output_config")
                resp = self._create_mapped(params)
            else:
                raise ProviderError(f"Anthropic rejected the request: {_msg(e)}") from e

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(
                f"Claude declined this request{f' ({category})' if category else ''}. Try rephrasing the prompt."
            )

        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args, err = parse_arguments(block.input)
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args, parse_error=err))
        u = resp.usage
        return ChatResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
            stop_reason=resp.stop_reason,
            raw=[b.to_dict() for b in resp.content],
            model=getattr(resp, "model", None) or self.model,
        )

    def _create(self, params: dict[str, Any]):
        try:
            if self.fallbacks:
                return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
            return self.client.messages.create(**params)
        except anthropic.BadRequestError:
            raise
        except anthropic.APIError as e:
            raise _map_error(e) from e

    def _create_mapped(self, params: dict[str, Any]):
        try:
            return self._create(params)
        except anthropic.BadRequestError as e:
            raise ProviderError(f"Anthropic rejected the request: {_msg(e)}") from e

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self.client.models.list())
        except anthropic.APIError as e:
            raise _map_error(e) from e


def _msg(e: anthropic.APIError) -> str:
    return getattr(e, "message", None) or str(e)


def _mentions(e: anthropic.APIError, *words: str) -> bool:
    m = _msg(e).lower()
    return any(w in m for w in words)


def _map_error(e: anthropic.APIError) -> ProviderError:
    if isinstance(e, anthropic.AuthenticationError):
        return ProviderError("Anthropic rejected the API key")
    if isinstance(e, anthropic.PermissionDeniedError):
        return ProviderError(f"Anthropic: permission denied - {_msg(e)}")
    if isinstance(e, anthropic.NotFoundError):
        return ProviderError(f"Anthropic: not found (check the model name) - {_msg(e)}")
    if isinstance(e, anthropic.RateLimitError):
        return ProviderError("Anthropic rate limit hit - try again shortly", retryable=True)
    if isinstance(e, anthropic.APITimeoutError):
        return ProviderError("Anthropic request timed out", retryable=True)
    if isinstance(e, anthropic.APIConnectionError):
        return ProviderError(f"Can't reach the Anthropic API: {_msg(e)}", retryable=True)
    if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
        return ProviderError(f"Anthropic server error ({e.status_code})", retryable=True)
    return ProviderError(f"Anthropic error: {_msg(e)}")
