"""Settings -> AI providers, and the model pricing table."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...llm.base import ProviderError
from ...llm.registry import (
    KINDS,
    ProviderConfig,
    build_provider,
    cost_hint,
    delete_provider,
    get_provider,
    list_providers,
    save_provider,
    set_default,
)
from ..deps import get_conn, templates

router = APIRouter(prefix="/settings")

COMMON = [("max_steps", "int", "Max tool-loop steps"),
          ("tool_patience", "int", "Auto mode: steps with an empty draft before switching to two-stage"),
          ("token_budget", "int", "Stop after this many input tokens (0 = no limit)")]
OPTION_FIELDS: dict[str, list[tuple]] = {
    "anthropic": [("effort", ["", "low", "medium", "high", "xhigh", "max"], "Effort"),
                  ("fallbacks", "bool", "Server-side refusal fallback")],
    "openai_compat": [("temperature", "float", "Temperature"),
                      ("max_output_tokens", "int", "Max output tokens"),
                      ("image_quality", ["", "low", "medium", "high", "xhigh", "max"], "Image quality (image models)")],
    "ollama": [("num_ctx", "int", "Context window (num_ctx)"), ("temperature", "float", "Temperature"),
               ("keep_alive", "str", "Keep model loaded"), ("think", ["", "true", "false"], "Thinking"),
               ("max_output_tokens", "int", "Max output tokens per call")],
}


def _form_ctx(cfg: ProviderConfig | None, kind: str) -> dict:
    kind = cfg.kind if cfg else kind
    defaults = KINDS[kind]
    fields = []
    for key, typ, label in OPTION_FIELDS[kind] + COMMON:
        val = cfg.options.get(key) if cfg else None
        fields.append({"key": key, "type": typ, "label": label, "value": "" if val is None else val,
                       "default": defaults["options"].get(key)})
    return {"cfg": cfg, "kind": kind, "kinds": KINDS, "defaults": defaults, "option_fields": fields}


def _parse_options(kind: str, form) -> dict:
    out = {}
    for key, typ, _ in OPTION_FIELDS[kind] + COMMON:
        if typ == "bool":
            out[key] = key in form
            continue
        raw = str(form.get(f"opt_{key}", "")).strip()
        if raw == "":
            continue
        try:
            if typ == "int":
                out[key] = int(raw)
            elif typ == "float":
                out[key] = float(raw)
            elif key == "think":
                out[key] = raw == "true"
            else:
                out[key] = raw
        except ValueError:
            raise HTTPException(400, f"{key}: expected a {typ}") from None
    return out


def _pricing(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM model_pricing ORDER BY model")]


@router.get("/providers")
def providers_page(request: Request, saved: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request, "providers.html",
        {"providers": (ps := list_providers(conn)), "hints": {p.id: cost_hint(conn, p) for p in ps}, "kinds": KINDS, "pricing": _pricing(conn), "saved": saved,
         **_form_ctx(None, "anthropic")},
    )


@router.get("/providers/form")
def provider_form(request: Request, kind: str = "anthropic", provider_id: int | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)):
    if kind not in KINDS:
        raise HTTPException(400, "unknown kind")
    cfg = get_provider(conn, provider_id) if provider_id else None
    return templates.TemplateResponse(request, "partials/provider_form.html", _form_ctx(cfg, kind))


@router.get("/providers/{provider_id}")
def edit_provider(provider_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    cfg = get_provider(conn, provider_id)
    if not cfg:
        raise HTTPException(404, "No such provider")
    return templates.TemplateResponse(request, "provider_edit.html", _form_ctx(cfg, cfg.kind))


@router.post("/providers/save")
async def save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    pid = int(form["id"]) if str(form.get("id", "")).isdigit() else None
    kind = str(form.get("kind", ""))
    if kind not in KINDS:
        raise HTTPException(400, "unknown kind")
    existing = get_provider(conn, pid) if pid else None
    api_key = str(form.get("api_key", "")).strip() or (existing.api_key if existing else "")
    if form.get("clear_api_key"):
        api_key = ""
    name = str(form.get("name", "")).strip() or KINDS[kind]["label"]
    model = str(form.get("model", "")).strip()
    if not model:
        raise HTTPException(400, "Model is required")
    cfg = ProviderConfig(
        id=pid, name=name, kind=kind, model=model, base_url=str(form.get("base_url", "")).strip().rstrip("/"),
        api_key=api_key, options=_parse_options(kind, form), tool_mode=str(form.get("tool_mode", "auto")),
        is_default=bool(form.get("is_default")),
    )
    try:
        save_provider(conn, cfg)
    except sqlite3.IntegrityError:
        raise HTTPException(400, f"A provider named {name!r} already exists") from None
    return RedirectResponse("/settings/providers?saved=1", status_code=303)


@router.post("/providers/{provider_id}/default")
def make_default(provider_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    set_default(conn, provider_id)
    return RedirectResponse("/settings/providers", status_code=303)


@router.post("/providers/{provider_id}/delete")
def remove(provider_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    delete_provider(conn, provider_id)
    return RedirectResponse("/settings/providers", status_code=303)


@router.post("/providers/{provider_id}/test")
def test(provider_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Lists the provider's models (free - no tokens spent) and checks the configured one exists."""
    cfg = get_provider(conn, provider_id)
    if not cfg:
        raise HTTPException(404, "No such provider")
    ctx: dict = {"cfg": cfg}
    try:
        models = build_provider(cfg).list_models()
        ctx.update(ok=True, models=models, has_model=cfg.model in models)
    except ProviderError as e:
        ctx.update(ok=False, error=str(e))
    return templates.TemplateResponse(request, "partials/provider_test.html", ctx)


@router.post("/pricing")
async def save_pricing(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    model = str(form.get("model", "")).strip()
    if not model:
        raise HTTPException(400, "Model is required")
    if form.get("delete"):
        conn.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
    else:
        def num(k, required=False):
            raw = str(form.get(k, "")).strip()
            if not raw:
                if required:
                    raise HTTPException(400, f"{k} is required")
                return None
            return float(raw)

        conn.execute(
            "INSERT INTO model_pricing(model, input_per_mtok, output_per_mtok, cache_read_per_mtok, "
            "cache_write_per_mtok, note) VALUES(?,?,?,?,?, 'user') ON CONFLICT(model) DO UPDATE SET "
            "input_per_mtok=excluded.input_per_mtok, output_per_mtok=excluded.output_per_mtok, "
            "cache_read_per_mtok=excluded.cache_read_per_mtok, cache_write_per_mtok=excluded.cache_write_per_mtok, "
            "note='user'",
            (model, num("input_per_mtok", True), num("output_per_mtok", True), num("cache_read_per_mtok"),
             num("cache_write_per_mtok")),
        )
    conn.commit()
    return RedirectResponse("/settings/providers#pricing", status_code=303)
