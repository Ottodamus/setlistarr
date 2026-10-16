# Setlistarr

AI-built Plex music playlists from the tracks you actually own. Type
*"90s alt-rock road trip, nothing too mellow, ~2 hours"*, get a draft built only from your
library, edit it, push it to Plex, and listen in Plexamp.

Status: **feature-complete v0.1** - Plex sync, library browser, stats, AI providers,
prompt -> draft generation with live progress, draft editing, push to Plex, history,
scheduled recipes, single-admin login.

## Run

```bash
docker compose up -d --build
```

Open `http://<host>:8787` and create the admin account (or set `ADMIN_USERNAME` /
`ADMIN_PASSWORD` in the environment), then:

1. **Settings → Plex**: server URL (the PMS itself, e.g. `http://192.168.0.x:32400`) and token →
   *Test connection & load libraries* → tick the music sections to use.
2. **System → Full sync**. Later syncs are incremental (hourly by default) with a nightly full
   sync to catch deletions.
3. Optional: add a Last.fm API key and MusicBrainz contact, then **Enrich tags** for richer
   genre/mood vocabulary.
4. **AI providers**: add one or more of
   - *Anthropic* - key from `ANTHROPIC_API_KEY` or the form; default model `claude-opus-5-5`.
   - *OpenAI / OpenAI-compatible* - OpenAI, LM Studio (`http://host:1234/v1`), etc.
   - *Ollama (native)* - `http://host:11434`; set `num_ctx` high enough for a tool loop (16k+).
   "Test" lists the provider's models without spending tokens.
5. **Generate**: type a prompt, optionally pin length / per-artist cap / years / exclusions.
6. **Edit the draft**: drag to reorder, remove, add tracks from a library search, lock tracks
   and *Regenerate* around them (optionally with a tweaked prompt).
7. **Push**: create a new Plex playlist, or replace one Setlistarr made before - in place, so
   it keeps its identity in Plexamp. Playlists you made yourself are never touched.
8. **Recipes**: save a prompt (or "Save as recipe" from a draft) with a schedule - daily, weekly,
   or any cron. Each run regenerates and replaces the recipe's playlist in place; "avoid repeats"
   keeps the previous refresh's tracks out. A failed run leaves the playlist as it was.

### How generation stays grounded

- **Tool mode** (default for capable models): the model searches your library with tools and
  builds the playlist through `add_to_draft`, which rejects anything that isn't a real,
  present track or that breaks a constraint - with a reason the model can act on.
- **Two-stage mode** (for local models with weak tool calling; `auto` falls back to it
  automatically): the model writes JSON search criteria using your real tag vocabulary,
  SQLite builds a diversified shortlist, and the model picks tracks *by number* from that list.
- Either way, a final validator re-checks every track, enforces length (±5%, min ±3 min),
  per-artist cap and exclusions, and backfills only from tracks the model was shown. Every change
  it makes is listed under the draft.
- Token usage and cost are logged per generation (Anthropic prices are seeded; add others under
  AI providers -> Pricing).

Persistent state lives in `/config` (`setlistarr.db`, `secret_key`).

| env | purpose |
|---|---|
| `TZ`, `PUID`, `PGID` | timezone for schedules; owner of files in `/config` |
| `PLEX_URL`, `PLEX_TOKEN`, `LASTFM_API_KEY`, `MUSICBRAINZ_CONTACT` | optional; pins the setting (UI read-only) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | used by providers whose key field is blank |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | optional; pins the admin login (otherwise first-run setup page) |
| `SETLISTARR_COOKIE_SECURE` | `1` when served over HTTPS (reverse proxy); leave off for plain-HTTP LAN |
| `SETLISTARR_AUTH_DISABLED` | `1` to turn login off, e.g. behind an auth proxy |
| `SETLISTARR_SECRET_KEY` | session signing key; generated if unset |
| `SETLISTARR_PORT` | default 8787 |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`; `json` (default) or `console` |

`GET /health` → JSON with DB, Plex and scheduler status (200 unless the app itself is broken).

## CLI / MCP

The image (or `pip install -e .`) also provides:

```bash
setlistarr sync [--full]     # same job the scheduler runs
setlistarr enrich [--limit N]
setlistarr generate "rainy sunday, mellow" [--provider NAME] [--mode auto|tools|two_stage] [--minutes 90]
setlistarr stats
setlistarr mcp               # MCP stdio server over the same DB, for Claude Desktop/Code
```

## Development

```bash
python -m venv ~/.venvs/setlistarr && ~/.venvs/setlistarr/bin/pip install -e '.[dev]'
~/.venvs/setlistarr/bin/python -m pytest -q
SETLISTARR_CONFIG_DIR=/tmp/pa LOG_FORMAT=console ~/.venvs/setlistarr/bin/setlistarr serve
```
