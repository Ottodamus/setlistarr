"""Tool-calling mode: the model explores the library with the shared tools and assembles the
playlist through draft tools that validate every track as it's added."""

from __future__ import annotations

import json
from typing import Any

from ..core.tools import TOOL_SCHEMAS, ToolContext, dispatch
from ..llm.base import ToolCall, ToolSpec
from .common import GenContext, ModeResult, ToolModeUnusable
from .constraints import Constraints
from .draft import DraftState
from .prompts import TOOL_AGENT_NUDGE, TOOL_AGENT_SYSTEM

# Library tools offered to the agent. No Plex writes, ever.
LIBRARY_TOOLS = [
    "list_facets", "list_genres", "search_tags", "search_tracks", "sample_tracks",
    "list_artists", "get_artist_tracks", "get_similar_artists", "get_track",
]
# Every tool result is written to the prompt cache once (the dominant cost on Claude), so keep
# results tight; the model can narrow or re-query.
MAX_ROWS_PER_RESULT = 40
EMPTY_DRAFT_PATIENCE = 12  # fallback when a provider doesn't set tool_patience
MAX_RESULT_CHARS = 16_000

_KEYS = {"type": "array", "items": {"type": "string"}}
DRAFT_TOOLS = [
    ToolSpec("add_to_draft",
             "Append tracks (by plex_key, in play order) to the playlist draft, or insert at a 0-based "
             "position. Each key is validated; rejected keys come back with a reason.",
             {"type": "object", "properties": {"plex_keys": _KEYS, "position": {"type": "integer"}},
              "required": ["plex_keys"]}),
    ToolSpec("remove_from_draft", "Remove tracks from the draft.",
             {"type": "object", "properties": {"plex_keys": _KEYS}, "required": ["plex_keys"]}),
    ToolSpec("reorder_draft", "Reorder the draft. Pass every current plex_key exactly once in the new order.",
             {"type": "object", "properties": {"plex_keys": _KEYS}, "required": ["plex_keys"]}),
    ToolSpec("get_draft", "The current draft: tracks, runtime vs. target, artists at their cap.",
             {"type": "object", "properties": {}}),
    ToolSpec("set_constraints",
             "Set constraints implied by the request. Fields the user locked are ignored.",
             {"type": "object", "properties": {
                 "target_minutes": {"type": "number"}, "max_per_artist": {"type": "integer"},
                 "exclude_artists": _KEYS, "exclude_tags": _KEYS,
                 "year_min": {"type": "integer"}, "year_max": {"type": "integer"}}}),
    ToolSpec("finish", "Finish: give the playlist a short title and 1-3 sentences of reasoning.",
             {"type": "object", "properties": {"title": {"type": "string"}, "reasoning": {"type": "string"}},
              "required": ["title", "reasoning"]}),
]


def tool_specs() -> list[ToolSpec]:
    lib = [
        ToolSpec(t["function"]["name"], t["function"]["description"], t["function"]["parameters"])
        for t in TOOL_SCHEMAS
        if t["function"]["name"] in LIBRARY_TOOLS
    ]
    return lib + DRAFT_TOOLS


def _keys(args: dict, field: str = "plex_keys") -> list[str]:
    val = args.get(field) or []
    if isinstance(val, (str, int)):
        val = [val]
    return [str(v) for v in val]


def _compact(result: dict) -> dict:
    tracks = result.get("tracks")
    if isinstance(tracks, list) and len(tracks) > MAX_ROWS_PER_RESULT:
        result = {**result, "tracks": tracks[:MAX_ROWS_PER_RESULT], "truncated": True}
    return result


class _Session:
    def __init__(self, gx: GenContext, constraints: Constraints):
        self.gx = gx
        self.c = constraints
        self.lib = ToolContext(conn=gx.conn)
        self.draft = DraftState(gx.conn, constraints)
        self.sources: dict[str, str] = {}
        self.title: str | None = None
        self.reasoning: str | None = None
        self.finished = False

    def execute(self, call: ToolCall) -> tuple[dict[str, Any], bool]:
        if call.parse_error:
            return {"error": call.parse_error}, True
        name, args = call.name, call.arguments or {}

        if name in LIBRARY_TOOLS:
            result = dispatch(self.lib, name, args)
            if isinstance(result.get("tracks"), list):
                self.draft.remember(t["plex_key"] for t in result["tracks"])
            result = _compact(result)
            n = result.get("count", len(result.get("tracks", []) or []))
            if "error" in result:
                self.gx.emit("tool", f"✗ {name}({_brief(args)}): {result['error'][:200]}")
                return result, True
            self.gx.emit("tool", f"{name}({_brief(args)})" + (f" → {n} results" if "tracks" in result or "artists" in result else ""))
            return result, False

        if name == "add_to_draft":
            keys = _keys(args)
            if not keys:
                return {"error": "plex_keys is required"}, True
            res = self.draft.add(keys, position=args.get("position"))
            for k in res["accepted"]:
                self.sources[k] = "model"
            msg = f"added {len(res['accepted'])} track(s) · {res['minutes']} min"
            if res["rejected"]:
                reasons = sorted({r["reason"].split(" (")[0] for r in res["rejected"]})
                msg += f" · rejected {len(res['rejected'])}: " + "; ".join(reasons)
            self.gx.emit("draft", msg, minutes=res["minutes"], tracks=res["track_count"])
            return res, not res["accepted"]

        if name == "remove_from_draft":
            res = self.draft.remove(_keys(args))
            self.gx.emit("draft", f"removed {res['removed']} track(s) · {res['minutes']} min")
            return res, False

        if name == "reorder_draft":
            res = self.draft.reorder(_keys(args))
            if "error" not in res:
                self.gx.emit("draft", "reordered the draft")
            return res, "error" in res

        if name == "get_draft":
            return self.draft.view(), False

        if name == "set_constraints":
            changed = self.c.update(args)
            ignored = sorted(k for k in args if k in self.c.locked)
            if changed:
                self.gx.emit("constraints", self.c.describe())
            return {"changed": changed, "ignored_locked": ignored, "constraints": self.c.describe()}, False

        if name == "finish":
            if not self.draft.tracks:
                return {"error": "the draft is empty - add tracks with add_to_draft before finishing"}, True
            self.title = (args.get("title") or "").strip() or None
            self.reasoning = (args.get("reasoning") or "").strip() or None
            self.finished = True
            return {"ok": True, **self.draft.totals()}, False

        return {"error": f"unknown tool {name!r}"}, True


def run_tool_agent(gx: GenContext, prompt: str, constraints: Constraints, seed: list[str] | None = None) -> ModeResult:
    s = _Session(gx, constraints)
    kept_note = ""
    if seed:
        s.draft.add(seed, source="user", force=True)
        s.draft.locked = set(s.draft.keys)
        for k in s.draft.keys:
            s.sources[k] = "user"
        kept_note = (
            "\n\nThe user kept these tracks; they're already in the draft and can't be removed "
            "(you may reorder around them):\n"
            + "\n".join(f"- {t['plex_key']}: {t['artist']} - {t['title']}" for t in s.draft.tracks)
        )
    specs = tool_specs()
    max_steps = int(gx.cfg.option("max_steps") or 30)
    patience = int(gx.cfg.option("tool_patience") or EMPTY_DRAFT_PATIENCE)
    budget = int(gx.cfg.option("token_budget") or 0)
    locked = ", ".join(sorted(constraints.locked)) or "none"
    if constraints.exclude_keys:
        kept_note += ("\n\nThis is a scheduled refresh: pass exclude_keys to your searches to skip the "
                      f"{len(constraints.exclude_keys)} tracks from the previous refresh "
                      "(add_to_draft rejects them):\n" + ",".join(constraints.exclude_keys))
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": f"Playlist request:\n{prompt}\n\nCurrent constraints: {constraints.describe()}\n"
                   f"Locked by the user: {locked}{kept_note}",
    }]
    nudges = 0
    error_streak = 0
    notes: list[str] = []

    for step in range(1, max_steps + 1):
        if budget and gx.meter.context_tokens > budget:
            notes.append(f"Stopped early: token budget ({budget:,}) reached")
            gx.emit("warn", notes[-1])
            break
        resp = gx.chat(system=TOOL_AGENT_SYSTEM, messages=messages, tools=specs, max_tokens=16000)
        messages.append(resp.as_message())
        if resp.text:
            gx.emit("model", resp.text[:600])

        if not resp.tool_calls:
            if s.draft.tracks:
                s.reasoning = s.reasoning or (resp.text[:800] or None)
                break
            nudges += 1
            if nudges >= 2:
                raise ToolModeUnusable("the model answered without calling tools")
            messages.append({"role": "user", "content": TOOL_AGENT_NUDGE})
            continue

        for call in resp.tool_calls:
            result, is_error = s.execute(call)
            error_streak = error_streak + 1 if is_error else 0
            messages.append({
                "role": "tool", "tool_call_id": call.id, "name": call.name, "is_error": is_error,
                "content": _serialize(result),
            })
        if s.finished:
            break
        if error_streak >= 6 and len(s.draft.tracks) <= len(seed or []):
            raise ToolModeUnusable("the model kept making invalid tool calls")
        if step >= patience and len(s.draft.tracks) <= len(seed or []):
            raise ToolModeUnusable(f"nothing added to the draft after {step} steps")
    else:
        notes.append(f"Stopped at the step limit ({max_steps})")
        gx.emit("warn", notes[-1])

    if len(s.draft.tracks) <= len(seed or []):
        raise ToolModeUnusable("the model finished without adding any tracks")
    return ModeResult(
        mode="tools", picks=s.draft.keys, pool=list(s.draft.pool), title=s.title,
        reasoning=s.reasoning, sources=s.sources, notes=notes,
    )


def _serialize(result: dict) -> str:
    text = json.dumps(result, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '..."(truncated)"'
    return text


def _brief(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if v in (None, "", [], False):
            continue
        s = ",".join(map(str, v)) if isinstance(v, list) else str(v)
        parts.append(f"{k}={s[:40]}")
    return ", ".join(parts)[:160]
