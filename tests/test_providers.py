"""Adapter wire-format tests against mock HTTP transports (no network)."""

import json

import anthropic
import httpx
import httpx2
import openai
import pytest

from setlistarr.llm.base import ProviderError, ToolSpec
from setlistarr.llm.claude import FALLBACK_BETA, ClaudeProvider
from setlistarr.llm.ollama import OllamaProvider
from setlistarr.llm.openai_compat import OpenAICompatProvider
from setlistarr.llm.registry import ProviderConfig, cost_usd, save_provider
from setlistarr.llm.base import Usage

TOOL = ToolSpec("search_tracks", "search", {"type": "object", "properties": {"any_tags": {"type": "array"}}})


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        status, payload = self.responses.pop(0)
        return (httpx2 if isinstance(request, httpx2.Request) else httpx).Response(status, json=payload)


CLAUDE_TOOL_TURN = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Searching."},
        {"type": "tool_use", "id": "toolu_1", "name": "search_tracks", "input": {"any_tags": ["rock"]}},
        {"type": "tool_use", "id": "toolu_2", "name": "list_facets", "input": {}},
    ],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
              "cache_creation_input_tokens": 20},
}


def claude(rec, **opts):
    client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(rec))
    return ClaudeProvider("claude-opus-5-5", api_key="sk-test", options={"effort": "medium", **opts},
                          http_client=client)


def test_claude_tool_loop_wire_format():
    rec = Recorder((200, CLAUDE_TOOL_TURN), (200, {**CLAUDE_TOOL_TURN, "content": [{"type": "text", "text": "done"}],
                                                   "stop_reason": "end_turn"}))
    p = claude(rec)
    r1 = p.chat(system="SYS", messages=[{"role": "user", "content": "hi"}], tools=[TOOL])
    assert [c.name for c in r1.tool_calls] == ["search_tracks", "list_facets"]
    assert r1.text == "Searching." and r1.usage.cache_read_tokens == 100 and r1.usage.cache_write_tokens == 20

    history = [{"role": "user", "content": "hi"}, r1.as_message(),
               {"role": "tool", "tool_call_id": "toolu_1", "name": "search_tracks", "content": "{}"},
               {"role": "tool", "tool_call_id": "toolu_2", "name": "list_facets", "content": "{}", "is_error": True}]
    p.chat(system="SYS", messages=history, tools=[TOOL])

    req = rec.requests[1]
    body = req["body"]
    assert FALLBACK_BETA in req["headers"]["anthropic-beta"] and body["fallbacks"] == "default"
    assert body["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert body["cache_control"] == {"type": "ephemeral"} and body["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in body
    # assistant turn echoed verbatim, thinking signature included
    assert body["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    # both tool results in ONE user message
    results = body["messages"][2]["content"]
    assert [b["tool_use_id"] for b in results] == ["toolu_1", "toolu_2"] and results[1]["is_error"] is True
    assert len(body["messages"]) == 3


def test_claude_refusal_and_auth_errors():
    refusal = {**CLAUDE_TOOL_TURN, "content": [], "stop_reason": "refusal",
               "stop_details": {"type": "refusal", "category": "cyber", "explanation": None}}
    with pytest.raises(ProviderError, match=r"declined.*cyber"):
        claude(Recorder((200, refusal))).chat(system="s", messages=[{"role": "user", "content": "x"}])
    err = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    with pytest.raises(ProviderError, match="rejected the API key"):
        claude(Recorder((401, err))).chat(system="s", messages=[{"role": "user", "content": "x"}])


def test_claude_structured_output_falls_back_when_schema_rejected():
    bad = {"type": "error", "error": {"type": "invalid_request_error", "message": "output_config.format.schema: unsupported"}}
    ok = {**CLAUDE_TOOL_TURN, "content": [{"type": "text", "text": '{"a": 1}'}], "stop_reason": "end_turn"}
    rec = Recorder((400, bad), (200, ok))
    p = claude(rec, fallbacks=False)
    r = p.chat(system="s", messages=[{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert r.text == '{"a": 1}'
    assert rec.requests[0]["body"]["output_config"]["format"]["type"] == "json_schema"
    assert rec.requests[1]["body"]["output_config"] == {"effort": "medium"}
    assert "anthropic-beta" not in rec.requests[0]["headers"]  # fallbacks off -> plain endpoint


OPENAI_TOOL_TURN = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-test",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "search_tracks", "arguments": '{"any_tags":["rock"]}'}},
                       {"id": "call_2", "type": "function", "function": {"name": "get_draft", "arguments": "{oops"}}]}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
              "prompt_tokens_details": {"cached_tokens": 40}},
}


def oai(rec, base_url="http://lmstudio.lan:1234/v1"):
    client = openai.DefaultHttpx2Client(transport=httpx2.MockTransport(rec))
    return OpenAICompatProvider("gpt-test", api_key=None, base_url=base_url, http_client=client)


def test_openai_compat_tool_calls_and_usage():
    rec = Recorder((200, OPENAI_TOOL_TURN), (200, OPENAI_TOOL_TURN))
    p = oai(rec)
    r = p.chat(system="SYS", messages=[{"role": "user", "content": "hi"}], tools=[TOOL])
    assert r.tool_calls[0].arguments == {"any_tags": ["rock"]}
    assert r.tool_calls[1].parse_error and "not valid JSON" in r.tool_calls[1].parse_error
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (60, 40, 10)

    p.chat(system="SYS", messages=[{"role": "user", "content": "hi"}, r.as_message(),
                                   {"role": "tool", "tool_call_id": "call_1", "name": "search_tracks", "content": "{}"}])
    msgs = rec.requests[1]["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == {"any_tags": ["rock"]}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "{}"}


def test_openai_compat_degrades_response_format():
    bad = {"error": {"message": "response_format json_schema is not supported", "type": "invalid_request_error"}}
    ok = {**OPENAI_TOOL_TURN, "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": '{"x":1}'}}]}
    rec = Recorder((400, bad), (400, bad), (200, ok))
    r = oai(rec).chat(system="s", messages=[{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert r.text == '{"x":1}'
    kinds = [(q["body"].get("response_format") or {}).get("type") for q in rec.requests]
    assert kinds == ["json_schema", "json_object", None]


def test_ollama_native_body_and_errors():
    resp = {"model": "qwen3", "message": {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "search_tracks", "arguments": {"any_tags": ["rock"]}}}]},
            "done_reason": "stop", "prompt_eval_count": 321, "eval_count": 12}
    rec = Recorder((200, resp), (404, {"error": "model 'nope' not found"}))
    p = OllamaProvider("qwen3", base_url="http://ollama.lan:11434", options={"num_ctx": 32768},
                       transport=httpx.MockTransport(rec))
    r = p.chat(system="s", messages=[{"role": "user", "content": "x"}], tools=[TOOL], json_schema={"type": "object"})
    body = rec.requests[0]["body"]
    assert body["options"]["num_ctx"] == 32768 and body["format"] == {"type": "object"} and body["stream"] is False
    assert body["options"]["num_predict"] == 2048  # capped, not the caller's 16000
    assert r.tool_calls[0].arguments == {"any_tags": ["rock"]} and r.usage.input_tokens == 321
    with pytest.raises(ProviderError, match="ollama pull qwen3"):
        p.chat(system="s", messages=[{"role": "user", "content": "x"}])


def test_cost(conn):
    paid = ProviderConfig(id=None, name="c", kind="anthropic", model="claude-opus-5-5")
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=2_000_000, cache_write_tokens=0)
    assert cost_usd(conn, paid, "claude-opus-5-5", u) == pytest.approx(4 + 2 + 0.4)
    assert cost_usd(conn, paid, "some-unpriced-model", u) is None
    local = ProviderConfig(id=None, name="o", kind="ollama", model="qwen3")
    lmstudio = ProviderConfig(id=None, name="l", kind="openai_compat", model="x", base_url="http://10.0.0.5:1234/v1")
    assert cost_usd(conn, local, "qwen3", u) == 0.0 == cost_usd(conn, lmstudio, "x", u)


def test_first_provider_becomes_default(conn):
    a = save_provider(conn, ProviderConfig(id=None, name="A", kind="ollama", model="m"))
    b = save_provider(conn, ProviderConfig(id=None, name="B", kind="ollama", model="m"))
    rows = dict(conn.execute("SELECT id, is_default FROM providers").fetchall())
    assert rows == {a: 1, b: 0}


IMG_OK = {"created": 0, "data": [{"b64_json": "iVBORw0KGgo="}], "usage": {"input_tokens": 10, "output_tokens": 20,
          "total_tokens": 30, "input_tokens_details": {"text_tokens": 10, "image_tokens": 0}}}


def img_provider(rec):
    client = openai.DefaultHttpx2Client(transport=httpx2.MockTransport(rec))
    return OpenAICompatProvider("gpt-image-2.5-flare", api_key="sk-x", base_url="https://api.openai.com/v1",
                                http_client=client)


def test_image_429_quota_is_explained_and_not_retried():
    quota = {"error": {"message": "You exceeded your current quota, please check your plan and billing details.",
                       "type": "insufficient_quota", "code": "insufficient_quota"}}
    rec = Recorder((429, quota), (429, quota), (429, quota))
    with pytest.raises(ProviderError, match="no credit left .*billing") as ei:
        img_provider(rec).generate_image("x")
    assert not ei.value.retryable and len(rec.requests) == 1


def test_image_429_rate_limit_retries_once_after_wait(monkeypatch):
    import setlistarr.llm.openai_compat as oc

    waits = []
    monkeypatch.setattr(oc.time, "sleep", waits.append)
    limited = {"error": {"message": "Rate limit reached for images per min", "type": "requests", "code": "rate_limit_exceeded"}}

    class R(Recorder):
        def __call__(self, request):
            resp = super().__call__(request)
            if resp.status_code == 429:
                resp.headers["retry-after"] = "7"
            return resp

    rec = R((429, limited), (200, IMG_OK))
    image, usage, model = img_provider(rec).generate_image("x")
    assert image.startswith(b"\x89PNG") and waits == [7.0] and len(rec.requests) == 2 and usage.output_tokens == 20

    rec2 = R((429, limited), (429, limited))
    with pytest.raises(ProviderError, match=r"rate limit hit: .*images per min - try again in ~7s"):
        img_provider(rec2).generate_image("x")
