# Setlistarr

AI-built Plex music playlists from the tracks you actually own. Type
*"90s alt-rock road trip, nothing too mellow, ~2 hours"*, get a draft built only from your
library, edit it, push it to Plex, and listen in Plexamp.

Status: **feature-complete v0.1** - Plex sync, library browser, stats, AI providers,
prompt -> draft generation with live progress, draft editing, push to Plex, history,
scheduled recipes, single-admin login.

## Run

```bash
git clone https://github.com/Ottodamus/setlistarr.git && cd setlistarr
docker compose up -d --build
```

Or in Portainer: *Stacks → Add stack → Repository*, URL `https://github.com/Ottodamus/setlistarr`,
compose path `docker-compose.yml`, and set any variables below under *Environment variables*.

**Set `ADMIN_USERNAME` / `ADMIN_PASSWORD` before the first start**, or open the app right away:
until an admin account exists, whoever reaches the setup page first creates it.

Then open `http://<host>:8787`:

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
8. **Themes**: Settings → Appearance - Dark (default), Light, Midnight or Synthwave.
9. **Recipes**: save a prompt (or "Save as recipe" from a draft) with a schedule - daily, weekly,
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

`GET /health` → `{"status": "ok" | "degraded" | "error"}` (200 unless the app itself is broken;
signed-in users get DB, Plex and scheduler details).

## Security

Setlistarr is built for a trusted home network.

- **Don't port-forward it to the internet.** For remote access put it behind a VPN (WireGuard,
  Tailscale) or a reverse proxy with HTTPS and set `SETLISTARR_COOKIE_SECURE=1`. Login protection
  is a single admin account with a basic slowdown on failed attempts.
- **Secrets live in `/config/setlistarr.db`** (Plex token, AI API keys - in plain text, like most
  *arr apps). The file is created owner-only (mode 600); keep the `/config` volume private and out
  of shared backups. Keys set as environment variables are never written to the DB.
- **Anyone who can sign in can spend your AI credits.** Each generation and each recipe run on a
  paid provider costs money; History shows the running total. Local Ollama models are free.
- Setlistarr only ever modifies Plex playlists it created itself; nothing is matched or deleted
  by title.

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
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
SETLISTARR_CONFIG_DIR=/tmp/setlistarr SETLISTARR_AUTH_DISABLED=1 LOG_FORMAT=console .venv/bin/setlistarr serve
```

Tests use a fake Plex server and a scripted fake LLM - no network, no API keys.

Versioning: every push bumps the minor version (`__version__` in `src/setlistarr/__init__.py`, the
only place it lives) and is tagged `vX.Y.0`. Enable the check with `git config core.hooksPath scripts/hooks`.
`CLAUDE.md` documents the architecture and the rules the code relies on.

Setlistarr was built with AI assistance (Anthropic's Claude); every change is covered by the
test suite and was verified against a real Plex server.

## License

[MIT](LICENSE)
