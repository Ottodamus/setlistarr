# Setlistarr - architecture and rules for contributors (and AI coding assistants)

Self-hosted, arr-style web app: natural-language prompt -> AI-built Plex music playlist using
**only tracks that exist in the user's library** -> pushed to Plex (consumed in Plexamp).
It grew out of an earlier CLI/MCP tool ("plex-dj"), whose tool layer and schema it inherits.

Put machine-specific notes (hosts, paths, credentials locations) in `CLAUDE.local.md`, which is
git-ignored - never in this file.

## Layout
- `src/setlistarr/core/` - DB, migrations, Plex client/sync/playlists, enrichment, LLM tool layer.
  The shared core for web, CLI and MCP.
- `services/` - operations used by web + scheduler + CLI (`jobs.run_job`, `library.do_sync`, browsing).
- `web/` - FastAPI + Jinja + HTMX. `routes/` one module per page; `templates/partials/` are HTMX fragments.
- `llm/` - provider adapters behind one neutral interface (`base.Provider.chat`):
  `claude.py` (anthropic SDK), `openai_compat.py` (openai SDK; OpenAI, LM Studio, any /v1),
  `ollama.py` (native /api/chat over httpx). `registry.py` = providers table, build, pricing.
- `generation/` - `tool_agent.py` (tool-calling mode), `two_stage.py` (criteria JSON -> SQL
  shortlist -> pick by number), `draft.py` (DraftState: the per-add grounding gate),
  `validate.finalize()` (the final gate), `runner.py` (background threads, SSE event log, persistence).
- `scheduler.py` - in-process APScheduler. `cli.py`, `mcp_server.py` - entry points.

## Hard rules
- **Grounding:** every track that reaches a draft or Plex must be a `plex_key` that exists in
  `tracks` and passes `LIVE_TRACK_SQL` (not removed, section enabled). Validate server-side; never
  trust a model-supplied key or title.
- **Locked (user-kept) tracks are sacred:** `finalize(keep=...)` adds them first (they count toward caps),
  never drops or trims them; `DraftState.remove` refuses them. User edits in the draft editor are not
  blocked by constraints - breaks show as warnings (`services.drafts.warnings`).
- **Every generation ends in `validate.finalize()`**, whatever the mode. Backfill only comes from
  the run's candidate pool (tracks a tool showed the model), never random library tracks, and every
  change is recorded in `drafts.adjustments_json` and shown in the UI.
- **The generation agent never writes to Plex.** Pushing is a user action or a recipe run. Only the
  MCP server exposes `create_plex_playlist` (gated by `allow_writes`).
- **Only touch Plex playlists Setlistarr created** - `managed_playlists` is the allow-list, checked in
  `services.push.push_draft` before any write. Never delete/replace by title match. Replace is in place
  (clear + add, same ratingKey) so Plexamp pins/artwork survive; if the playlist was deleted in Plex it's
  re-created and the managed row swapped.
- **Single uvicorn worker.** The scheduler is in-process; more workers = duplicate jobs.
- **Library-writing jobs go through `services.jobs.run_job`** (shared lock + `jobs` row).
- **DB: raw `sqlite3`, no ORM.** Schema changes = new `core/migrations/NNNN_name.sql`, tracked by
  `PRAGMA user_version`. Never edit a migration that has shipped (0001-0005 have now shipped).
- **No python-plexapi.** Its partial objects silently reload per item when an attribute is None
  (N+1 requests during sync). Use `core/plex/client.PlexClient` (httpx, JSON). When adding a new
  Plex endpoint, check python-plexapi's source for the exact request shape first, don't guess.
- **Recipes** (`services/recipes.run_recipe`): generate synchronously in the scheduler thread, then push
  to the recipe's managed playlist (create on first success, replace after). A failed generation never
  touches Plex. Recipe runs do NOT take the library lock (a slow LLM must not block syncs) but a recipe
  never overlaps itself. `avoid_repeats` feeds the last run's keys into `Constraints.exclude_keys`.
- **Auth** (`web/auth.py`): one admin, scrypt hashes, signed session cookie, SameSite=Lax + Origin check
  on non-GET. Public: `/health`, `/login`, `/setup`, `/static/*`. `ADMIN_USERNAME`/`ADMIN_PASSWORD` env
  pin the account; `SETLISTARR_AUTH_DISABLED=1` turns auth off (tests set this in conftest).
- `/health` returns 200 when the app+DB work even if Plex is down (status `degraded`) so Docker
  doesn't restart-loop on a Plex outage.
- Settings: env var (if defined for that setting) > `settings` table > default. Secrets are never
  rendered back into forms; blank secret field on save = keep current value.

## LLM conventions
- Neutral history (see `llm/base.py`) is **append-only** and stays on one provider. Assistant
  turns carry `raw` (Anthropic: full content blocks incl. thinking + signature) and are echoed
  back verbatim - required by preserved thinking on current Claude models. Never edit or delete
  earlier turns; nudges are appended as new user messages.
- System prompts in `generation/prompts.py` are static strings (prompt caching is a prefix
  match). Put per-request data in the first user message, never in the system prompt.
- Claude adapter: default model `claude-opus-5-5`; explicit cache breakpoint on system + top-level
  auto caching; `fallbacks="default"` (beta `server-side-fallback-2026-07-01`) on unless the
  provider option disables it; `effort` via `output_config`; no forced `tool_choice` (400 on
  current models); `stop_reason == "refusal"` -> ProviderError. Check the claude-api skill before
  changing any of this.
- Structured output is best-effort everywhere (Anthropic `output_config.format`, OpenAI
  `response_format` json_schema -> json_object -> none, Ollama `format`); callers always parse
  with `extract_json` and validate.
- Cost = `model_pricing` x usage per call, priced at the model that actually served it
  (`resp.model`). Local providers cost 0; a paid model without a row logs cost NULL (shown "?").
- Both SDKs are httpx2-based: in tests inject `anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(...))`
  / `openai.DefaultHttpx2Client(...)`. The Ollama adapter and Plex client use plain httpx.

## Data model notes
- `tracks.artist_name` = album artist (Plex grandparentTitle); `tracks.track_artist` = Plex
  originalTitle (set only when different, e.g. compilations). Display/match with
  `COALESCE(track_artist, artist_name)`.
- `track_tags` is the single tag vocabulary the model filters on. Sources: `plex-genre|plex-style|
  plex-mood` (rebuilt by sync from artist+album+track tags) and `lastfm-artist|lastfm-track|
  musicbrainz` (from enrich, never touched by sync).
- Tracks gone from Plex are soft-deleted (`removed_at`) so history/drafts still render.
- Incremental sync = `updatedAt>>` + `lastViewedAt>>` (plays don't bump updatedAt) with a 5 min
  overlap; then a count check escalates to full sync if Plex has fewer tracks than we do.

## Dev loop
- `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- Tests: `.venv/bin/python -m pytest -q`. No network or API keys needed.
  Fake Plex = `tests/fakes/fake_plex.py` on pytest-httpserver; extend it when adding endpoints.
  Fake LLM = `tests/fakes/scripted_provider.py` (inject via `manager.start(..., inject={"provider": p})`).
- Run locally: `SETLISTARR_CONFIG_DIR=/tmp/setlistarr SETLISTARR_AUTH_DISABLED=1 LOG_FORMAT=console setlistarr serve`.

## Conventions
- Match surrounding style: type hints, `from __future__ import annotations`, small modules,
  structlog `log.info("area.event", key=value)` event names.
- UI: server-rendered Jinja; HTMX for partial updates (`is_htmx(request)` -> return a partial).
  Vendored JS in `web/static/vendor/` (htmx 2.0.4, htmx-ext-sse 2.2.2, SortableJS 1.15.6) - no CDN, no Node.
- Colors are CSS custom properties in `web/static/app.css` - never hard-code a color. Themes (dark default,
  light, midnight, synthwave) override the tokens via `<html data-theme>`, rendered server-side from the
  `ui_theme` setting. A new theme must define every token (a test checks) and pass WCAG AA contrast.
- Errors shown to the user are `PlexError` / `ProviderError` messages, never tracebacks.
