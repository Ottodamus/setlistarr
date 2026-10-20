# Setlistarr

AI-built Plex music playlists from the tracks you actually own. Type
*"90s alt-rock road trip, nothing too mellow, ~2 hours"*, get a draft built only from your
library, edit it, give it a cover, push it to Plex, and listen in Plexamp.

- Plex sync with a local library cache (tracks, artists, tags, your Plex playlists)
- Claude, ChatGPT / any OpenAI-compatible API, or local Ollama models
- Every track is checked against your library - the AI can't invent songs
- Draft editor: reorder, remove, add, lock tracks and regenerate around them
- Playlist covers: upload & crop, or generate with AI
- Recipes: saved prompts that refresh a Plex playlist on a schedule
- Discover: artists you don't own yet (Last.fm or ListenBrainz), sent to Lidarr when you approve, and a
  rolling Discover Weekly / Monthly playlist
- Arr-style web UI with themes, single admin login, Docker / Portainer

**Contents:** [Install](#install) · [First-run setup](#first-run-setup) ·
[Tag enrichment (MusicBrainz & Last.fm)](#tag-enrichment-musicbrainz--lastfm) ·
[AI providers](#ai-providers) · [Discover & Lidarr](#discover--lidarr) · [Using it](#using-it) · [Updating](#updating) ·
[Configuration reference](#configuration-reference) · [Troubleshooting](#troubleshooting) ·
[Security](#security) · [CLI / MCP](#cli--mcp) · [Development](#development)

---

## Install

### Portainer (recommended)

1. *Stacks → Add stack*, name it `setlistarr`.
2. **Build method: Repository**
   - Repository URL: `https://github.com/Ottodamus/setlistarr`
   - Reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
3. Under **Environment variables** add at least:

   | Name | Example | Why |
   |---|---|---|
   | `TZ` | `America/New_York` | Schedules (nightly sync, recipes) run in this time zone. Default is UTC. |
   | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / *a long password* | Your login. Without them, the first person to open the app creates the account. |
   | `PUID` / `PGID` | `1000` / `1000` | Owner of the files in the config folder on the host. |

   Optional: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LASTFM_API_KEY`, `PLEX_URL`, `PLEX_TOKEN` -
   see [Configuration reference](#configuration-reference). Anything not set here can be entered
   in the web UI instead.
4. **Deploy the stack.** The first build takes a minute or two.
5. Open `http://<docker-host>:8787`.

Data lives in `/opt/docker/setlistarr/config` on the host (change with `CONFIG_PATH`).

### Docker Compose

```bash
git clone https://github.com/Ottodamus/setlistarr.git && cd setlistarr
cat > .env <<'EOF'
TZ=America/New_York
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me-to-something-long
EOF
docker compose up -d --build
```

---

## First-run setup

### 1. Sign in

Use `ADMIN_USERNAME` / `ADMIN_PASSWORD`, or create the account on the setup page if you didn't
set them. Change the password later under **Settings → Admin password**.

### 2. Connect Plex

**Settings → Plex**

- **Plex server URL** - the server itself, e.g. `http://192.168.1.10:32400`. A LAN address is
  faster than a public/Cloudflare URL.
- **Plex token** - how to find it:
  1. Open Plex Web and go to any track or album.
  2. Click **⋯ → Get Info → View XML**.
  3. In the new tab's address bar, copy the value after `X-Plex-Token=`.
- Click **Test connection & load libraries**, tick your music library (leave e.g. audiobooks
  unticked), then **Save libraries**.

### 3. Sync

**System → Full sync.** A ~13,000-track library takes about 20 seconds. After that:

- **Sync now / hourly sync** (incremental) picks up added or edited tracks, plays, ratings and
  your Plex playlists in a second or two.
- **Full sync** (nightly at 3:30 in your `TZ` by default) re-reads everything and removes
  deleted tracks. Change times under **Settings → Sync schedule**.

Results for every run are listed under **System → Jobs**.

---

## Tag enrichment (MusicBrainz & Last.fm)

The AI picks tracks by tags that exist in your library. Plex provides genres, styles and moods;
two free services add much more:

| | MusicBrainz | Last.fm |
|---|---|---|
| What it adds | Curated **genres** per artist (*post-grunge*, *alternative metal*…) | **Tags** per artist *and* per track (*road trip*, *90s*, *melancholy*, *workout*…) and **similar artists** |
| Account | None - just a contact email (MusicBrainz requires one in requests) | Free API key |
| Speed | 1 request/second, so ~3 min per 150 artists the first time | Fast, but per-track tags mean one request per track the first time (~15-30 min for 13k tracks) |

Last.fm is the bigger win: its tags describe vibe and use, which is how you write prompts, and
its similar-artists data lets the AI branch out ("more like Radiohead") using only artists you own.

### Set up MusicBrainz

1. **Settings → Enrichment → MusicBrainz contact**: enter an email address (or a URL). It's only
   sent to MusicBrainz so they can contact you if something misbehaves.
2. **Save settings.**

### Set up Last.fm

1. Sign in (or sign up, free) at [last.fm](https://www.last.fm), then open
   **<https://www.last.fm/api/account/create>**.
2. Fill in the form - nothing is verified:
   - Application name: `Setlistarr`
   - Application description: `Self-hosted playlist builder`
   - Callback URL / homepage: leave empty
3. Submit and copy the **API key** (you don't need the shared secret).
4. **Settings → Enrichment → Last.fm API key**: paste it and **Save settings**
   (or set `LASTFM_API_KEY` in the stack's environment variables).

### Run it

- **System → Enrich tags** runs MusicBrainz and Last.fm together in one pass (artist by artist).
- It also runs automatically after every nightly full sync (*Enrich after full sync*).
- Everything is cached for 30 days, so after the first run only new artists/tracks are fetched.
  Failed requests aren't cached and are retried next time.
- **Fetch per-track Last.fm tags** gives the best results but makes the first run slow; turn it
  off for artist-level tags only.
- Library jobs take turns: if you start *Enrich tags* while a sync runs (or the other way
  round), the second is skipped - just start it again afterwards. The rest of the app keeps
  working while enrichment runs.
- Check coverage under **Stats** (*Last.fm enriched*, *Last.fm / MusicBrainz tags*).

---

## AI providers

**AI providers → Add a provider.** Add as many as you like; you pick one per playlist. **Test**
lists the provider's models without spending anything.

### Claude (Anthropic)

- Kind: **Anthropic (Claude)**. Get a key at [console.anthropic.com](https://console.anthropic.com)
  (paste it in the form or set `ANTHROPIC_API_KEY`).
- Default model `claude-opus-5-5` gives the best playlists (about $0.30 per playlist).
  `claude-sonnet-5-5` or a lower **Effort** costs less.
- Mode **auto** (tool calling).

### ChatGPT / OpenAI and compatible servers

- Kind: **OpenAI / OpenAI-compatible**, base URL `https://api.openai.com/v1`, key from
  [platform.openai.com](https://platform.openai.com/api-keys) (or `OPENAI_API_KEY`).
- Works the same way with LM Studio, vLLM, llama.cpp server etc. (`http://host:port/v1`).
- **API billing is separate from a ChatGPT subscription** - the account needs prepaid credit or a
  payment method, otherwise every request fails with "no credit left (insufficient_quota)".
- Add the model's price under **AI providers → Pricing** to track cost (Anthropic and OpenAI
  image prices are pre-filled).

### Image models (for AI covers)

- Add another **OpenAI / OpenAI-compatible** provider whose model is an image model, e.g.
  `gpt-image-2.5-flare` (fast) or `gpt-image-2.5-sunburst` (most capable). It's used for covers
  only, never for building playlists. Set **Image quality** per provider (default *medium*).
- `chatgpt-image-latest`, `gpt-image-1` and `gpt-image-1.5` are retired by OpenAI on
  **2026-12-01** - Setlistarr shows a warning if you use them.
- Without an image model, Claude / ChatGPT / Ollama can still make covers: they draw them as
  vector art (shapes, gradients, simple type).

### Ollama (local, free)

- Kind: **Ollama (native)**, base URL `http://<ollama-host>:11434`, model e.g. `llama3.1:8b`.
- New Ollama providers default to **two-stage** mode - two model calls instead of a long tool
  loop, the right choice for models under ~30B. Bigger models (e.g. 32B+) can try *auto*.
- **Ollama must listen on the network**, not just localhost:
  - macOS app: `launchctl setenv OLLAMA_HOST 0.0.0.0:11434`, then quit Ollama from the menu bar
    and reopen it. Allow incoming connections if macOS asks.
  - Linux (systemd): `sudo systemctl edit ollama`, add
    `[Service]` / `Environment="OLLAMA_HOST=0.0.0.0:11434"`, then `sudo systemctl restart ollama`.
  - Check with `curl http://<ollama-host>:11434/api/tags` from the Docker host.

---

## Discover & Lidarr

Discover finds artists you **don't** have yet, sends the ones you approve to Lidarr, and builds a
**Discover Weekly** (or **Monthly**) playlist from what arrives. Set it up under
**Discover → Discover settings**.

### 1. Pick a recommendation source

| | Last.fm similar artists | ListenBrainz recommendations |
|---|---|---|
| How | For the artists you play most, Last.fm lists similar artists; ones you don't own are ranked by how many of *your* artists point to them | Personal recommendations from your actual listening history, refreshed by ListenBrainz about weekly |
| Needs | The Last.fm API key ([setup](#set-up-lastfm)) and one *Enrich tags* run | A ListenBrainz token (below) and a few weeks of plays sent to ListenBrainz |
| Feels like | "More like what I already love" | Spotify-style Discover Weekly |

### 2. Connect Lidarr

1. In Lidarr: **Settings → General → Security → API Key** - copy it.
2. In Setlistarr: **Discover settings → Lidarr**, enter the Lidarr URL (e.g. `http://192.168.1.10:8686`)
   and the API key, click **Test connection & load folders** (or set `LIDARR_URL` / `LIDARR_API_KEY`).
3. Pick the **root folder**, **quality profile** and **metadata profile** new artists should use, then **Save**.
4. Recommended: in Lidarr, **Settings → Connect → + → Plex Media Server**, so Plex scans new downloads right
   away and Setlistarr sees them on its next sync.

When you approve an artist, Setlistarr adds it to Lidarr with **only its most popular album** monitored
and searched - no whole discographies and no future releases. The album is picked from ListenBrainz
listen counts (needs a ListenBrainz token) or Last.fm's top albums matched on MusicBrainz, otherwise
Lidarr's "latest album" option is used. The album is picked when the suggestion is found and shown in
**Waiting for your OK** (with a MusicBrainz link), so you see exactly what will be downloaded before you
approve it.

### 3. ListenBrainz (needed for the ListenBrainz source; optional otherwise)

1. Sign up at [listenbrainz.org](https://listenbrainz.org) (uses a MusicBrainz account), then open
   **your profile → Settings → User token** and copy it.
2. **Discover settings → ListenBrainz**: paste the token, click **Test token** (or set `LISTENBRAINZ_TOKEN`).
3. Tick **Send my Plex plays to ListenBrainz** and choose the **Plex account** (the server owner by default).
   After each library sync that account's music plays are sent; the first time, all the history Plex has
   kept for it. Other people's plays on your server are never sent. Don't also run another ListenBrainz
   scrobbler for Plex, or plays are counted twice.

### 4. Choose how it runs

- **Discover playlist**: *Off* (manual), *Discover Weekly* (Mondays 6:00) or *Discover Monthly* (the 1st, 6:00).
- **Send new picks to Lidarr automatically**: **off by default** - nothing is downloaded until you click
  *Approve* on the Discover page. Turn it on to send every new pick straight to Lidarr.
- **New artists per run**, **playlist length**, **arrivals window** (how many weeks of new music the
  playlist includes) and the **playlist cover** (don't touch / keep one / new AI cover every run).

### How the Discover playlist is built

Each run (or **Run now**): finds new suggestions → sends approved ones to Lidarr → notices which requested
artists have arrived in Plex (also checked after every sync) → rebuilds **one** playlist that Setlistarr
creates itself and then replaces in place: up to 3 tracks by each artist that arrived in the last few
weeks (the album Lidarr fetched first), **topped up** with never-played tracks from your library that
match what you play most, so it's never empty while downloads are pending. Artists are spread out so the
same one doesn't play twice in a row. Open it from the Discover page to edit it or change its cover.

**Sync now** on the Discover page runs a library sync, which checks whether requested albums have
arrived in Plex (and sends plays to ListenBrainz if that's on) without waiting for the scheduled sync.

---

## Using it

- **Generate** - type a prompt, pick a provider. Optional: fixed constraints (length, max per
  artist, years, excluded artists/tags) and **Cover (optional)** to make a cover in the same run.
  Progress is shown live.
- **Edit the draft** - drag to reorder, ✕ to remove, search to add tracks, 🔒 to lock tracks,
  then *Regenerate* around the locked ones (optionally with a tweaked prompt).
- **Cover** - upload a picture and crop it to a square (live preview), or generate one with AI.
  Covers can be downloaded, and you can switch back to earlier ones.
- **Push** - create a new Plex playlist, or replace one Setlistarr made before *in place*
  (same playlist in Plexamp, pins and artwork survive). Playlists you made yourself are never touched.
- **Recipes** - save a prompt (or *Save as recipe* on a draft) with a schedule: daily, weekly,
  twice a week, monthly or any cron. Each run regenerates and replaces the recipe's playlist;
  *Avoid repeats* keeps the previous refresh's tracks out. Covers: don't touch, keep one, or make
  a new AI cover every refresh. A failed run never changes the playlist.
- **Library** - browse and search tracks/artists; see which Plex playlists each track or artist
  is in and filter by playlist.
- **History** - every run with provider, tokens, cost and push status, plus total AI spend.
- **Themes** - Settings → Appearance: Dark (default), Light, Midnight, Synthwave.

### How generation stays grounded

- **Tool mode** (Claude, ChatGPT, big local models): the model searches your library with tools
  and builds the playlist through `add_to_draft`, which rejects anything that isn't a real
  track or that breaks a constraint - with a reason the model can act on.
- **Two-stage mode** (small local models; *auto* falls back to it): the model writes search
  criteria using your real tag vocabulary, SQLite builds a shortlist, and the model picks tracks
  *by number* from that list.
- Either way a final validator re-checks every track, enforces length (±5%, min ±3 min),
  per-artist cap and exclusions, and only tops up from tracks the model was shown. Every change
  it makes is listed under the draft.

---

## Updating

Portainer: open the stack → **Pull and redeploy** (or enable *GitOps updates* to redeploy
automatically on new commits). Compose: `git pull && docker compose up -d --build`. Your data in
the config folder is kept and the database upgrades itself. The version is shown at the bottom of
the sidebar.

---

## Configuration reference

All settings can be changed in the web UI; an environment variable **pins** a setting (the UI
field becomes read-only).

| Variable | Purpose |
|---|---|
| `TZ` | Time zone for schedules (default `UTC`) |
| `PUID`, `PGID` | Owner of files in `/config` (default 1000) |
| `CONFIG_PATH` | Host folder for `/config` (default `/opt/docker/setlistarr/config`) |
| `SETLISTARR_PORT` | Host port (default 8787) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Admin login; otherwise first-run setup page |
| `PLEX_URL`, `PLEX_TOKEN` | Plex server and token |
| `LASTFM_API_KEY`, `MUSICBRAINZ_CONTACT` | Tag enrichment |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Used by providers whose key field is blank |
| `LIDARR_URL`, `LIDARR_API_KEY` | Lidarr connection for Discover |
| `LISTENBRAINZ_TOKEN` | ListenBrainz recommendations, popularity data and sending plays |
| `SETLISTARR_COOKIE_SECURE` | `1` when served over HTTPS (reverse proxy); leave `0` for plain-HTTP LAN |
| `SETLISTARR_AUTH_DISABLED` | `1` turns the login off (e.g. behind an auth proxy) |
| `SETLISTARR_SECRET_KEY` | Session signing key; generated and stored in `/config/secret_key` if unset |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`; `json` (default) or `console` |

Persistent state: `/config/setlistarr.db` (settings, library cache, history), `/config/covers/`,
`/config/secret_key`.

`GET /health` returns `{"status": "ok" | "degraded" | "error"}` - 200 unless the app itself is
broken (Docker's health check uses it). Signed-in users get DB, Plex and scheduler details.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Schedules run at the wrong hour | Set `TZ` in the stack and redeploy. **System** shows next run times with their time zone. |
| "Plex rejected the token" | Copy a fresh token (see [Connect Plex](#2-connect-plex)). |
| Ollama: "Can't reach Ollama" / connection refused | Ollama is only listening on localhost - see [Ollama](#ollama-local-free). |
| OpenAI: "no credit left (insufficient_quota)" | Add credit / a payment method at platform.openai.com → Settings → Billing. |
| OpenAI: "rate limit hit" | Wait the time shown; covers retry once automatically. |
| Covers: "refused this cover prompt" | Reword it - image models won't draw real artists, bands or people. |
| A playlist is too short ("min short of the target") | Your library has too few matching tracks; loosen the prompt or constraints. |
| Small local model picks odd tracks | Use two-stage mode, a bigger model, or Claude/ChatGPT for important playlists. |
| *Enrich tags* shows "skipped" | Another library job was running; start it again when it's done. |
| Discover: "Couldn't identify this artist on MusicBrainz" | The name didn't match a MusicBrainz artist closely enough; dismiss it, or add the artist in Lidarr yourself. |
| Discover: ListenBrainz "no recommendations yet" | Keep *Send my Plex plays* on; ListenBrainz needs a few weeks of history. Use the Last.fm source meanwhile. |
| Discover: an artist stays in "Sent to Lidarr" | Lidarr hasn't downloaded it yet, or Plex hasn't picked it up - check Lidarr's queue, then click **Sync now** on the Discover page. Adding Plex under Lidarr → Settings → Connect makes Plex scan new downloads straight away. |

---

## Security

Setlistarr is built for a trusted home network.

- **Don't port-forward it to the internet.** For remote access put it behind a VPN (WireGuard,
  Tailscale) or a reverse proxy with HTTPS and set `SETLISTARR_COOKIE_SECURE=1`. Login protection
  is a single admin account with a basic slowdown on failed attempts.
- **Secrets live in `/config/setlistarr.db`** (Plex token, AI API keys - in plain text, like most
  *arr apps). The file is created owner-only (mode 600); keep the config folder private and out
  of shared backups. Keys set as environment variables are never written to the DB.
- **Anyone who can sign in can spend your AI credits.** Each generation, cover and recipe run on a
  paid provider costs money; History shows the running total. Local Ollama models are free.
- Setlistarr only ever modifies Plex playlists it created itself; nothing is matched or deleted
  by title. Your other Plex playlists are only read.

---

## CLI / MCP

The image (or `pip install -e .`) also provides:

```bash
setlistarr sync [--full]     # same job the scheduler runs
setlistarr enrich [--limit N]
setlistarr generate "rainy sunday, mellow" [--provider NAME] [--mode auto|tools|two_stage] [--minutes 90]
setlistarr stats
setlistarr mcp               # MCP stdio server over the same DB, for Claude Desktop/Code
```

In Docker: `docker exec -it setlistarr setlistarr sync`.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
SETLISTARR_CONFIG_DIR=/tmp/setlistarr SETLISTARR_AUTH_DISABLED=1 LOG_FORMAT=console .venv/bin/setlistarr serve
```

- Tests use a fake Plex server and a scripted fake LLM - no network, no API keys.
- `python scripts/ui_audit.py` (needs `pip install playwright && playwright install chromium`)
  renders every page from 1440 px down to 390 px and fails on layout problems.
- Versioning: every push bumps the minor version (`__version__` in `src/setlistarr/__init__.py`,
  the only place it lives) and is tagged `vX.Y.0`. Enable the check with
  `git config core.hooksPath scripts/hooks`.
- `CLAUDE.md` documents the architecture and the rules the code relies on.

Setlistarr was built with AI assistance (Anthropic's Claude); every change is covered by the
test suite and was verified against a real Plex server.

## License

[MIT](LICENSE)
