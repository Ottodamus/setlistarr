"""Render every Setlistarr page at several widths and report layout problems: elements sticking
out of their card, pages that scroll sideways, collapsed table columns, and tables that need
horizontal scrolling at desktop width. Seeds a throwaway DB with deliberately long names.

Dev-only. Needs:  pip install playwright && playwright install chromium
Usage:            python scripts/ui_audit.py [--shots]   (screenshots go to ./ui-audit-out/)
Exit code 1 if anything is found.
"""
import io
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.getcwd(), "ui-audit-out")
sys.path.insert(0, REPO)
cfg_dir = tempfile.mkdtemp(prefix="sl-audit-")
os.environ.update(SETLISTARR_CONFIG_DIR=cfg_dir, SETLISTARR_AUTH_DISABLED="1", SETLISTARR_DISABLE_SCHEDULER="1",
                  LOG_LEVEL="WARNING", LOG_FORMAT="console")

from PIL import Image  # noqa: E402

from setlistarr.config import bootstrap  # noqa: E402
from setlistarr.core.db import connect, migrate  # noqa: E402
from setlistarr.generation.constraints import Constraints  # noqa: E402
from setlistarr.generation.runner import GenerationManager  # noqa: E402
from setlistarr.llm.registry import ProviderConfig, save_provider  # noqa: E402
from setlistarr.services import covers, recipes  # noqa: E402
from setlistarr.services.generations import get_generation  # noqa: E402
from setlistarr.services.push import register_managed  # noqa: E402
from tests.fakes.scripted_provider import ScriptedProvider, call, tools  # noqa: E402

# ------------------------------------------------------------------ seed
conn = connect(bootstrap().db_path)
migrate(conn)
conn.execute("INSERT INTO libraries(section_id, name, enabled, track_count, last_sync_at) VALUES(11, 'Music', 1, 40, datetime('now'))")
LONG = [
    ("in_the_dark.ech", "Bring Me the Horizon & Kanisan", "Lo-files"),
    ("The Needle", "Tech N9ne featuring Krizz Kaliko", "The Storm"),
    ("Fired", "Tech N9ne feat. Stevie Stone & Darrein Safron", "Strangeulation Vol. II"),
    ("Gimme! Gimme! Gimme! (A Man After Midnight) [Remastered 2014 Deluxe Edition]", "ABBA", "Gold: Greatest Hits"),
    ("Supercalifragilisticexpialidocious_extended_mix_final_v2", "A Very Long Artist Name That Keeps Going", "Album"),
]
for i in range(40):
    t, a, al = LONG[i % len(LONG)]
    conn.execute(
        "INSERT INTO tracks(plex_key, title, artist_name, artist_key, track_artist, album_title, year, duration_ms, "
        "section_id, play_count, user_rating, listeners) VALUES(?,?,?,?,?,?,?,?,11,?,?,?)",
        (str(1000 + i), f"{t} {i}", a, f"artist-{i % len(LONG)}", a, al, 1990 + i % 30, 200_000 + i * 1000, i % 7,
         8 if i % 3 else None, (i * 97_531) % 3_100_000 if i % 4 else None))
    conn.execute("INSERT INTO track_tags(track_id, source, tag) VALUES(?, 'plex-genre', 'alternative/indie rock')", (i + 1,))
for pk, title in [("901", "Road Trip Mix"), ("902", "Late Night Coding Session Deep Focus Instrumentals"),
                  ("903", "Gym"), ("904", "Sunday Morning Coffee & Crossword Vibes")]:
    conn.execute("INSERT INTO plex_playlists(rating_key, title, synced_at) VALUES(?, ?, datetime('now'))", (pk, title))
    conn.executemany("INSERT INTO playlist_tracks(playlist_key, position, plex_key) VALUES(?,?,?)",
                     [(pk, j, str(1000 + i)) for j, i in enumerate(range(int(pk) - 901, 40, 2))])
for i, (name, status, why) in enumerate([
        ("Cocteau Twins", "suggested", ["A Very Long Artist Name That Keeps Going", "ABBA", "Bring Me the Horizon & Kanisan"]),
        ("The Brian Jonestown Massacre Featuring A Long Guest List", "suggested", ["ABBA"]),
        ("Ride", "requested", []), ("ABBA", "arrived", []), ("Unknown Artist", "failed", []), ("Nickelback", "blocked", []),
        *((f"Filler Artist {n}", "suggested", ["ABBA"]) for n in range(12))]):
    conn.execute("INSERT INTO discoveries(ident, artist_name, artist_mbid, source, score, reasons_json, status, album_title, "
                 "album_mbid, album_how, album_checked_at, "
                 "note, first_seen_at, updated_at, requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),?,datetime('now'),datetime('now'),datetime('now'))",
                 (f"mbid-{i}", name, f"mbid-{i}", "lastfm", 3.2 - i, json.dumps(why), status,
                  "Heaven or Las Vegas (2014 Remastered Deluxe Anniversary Edition)" if i != 1 else None,
                  f"rg-{i}" if i != 1 else None, "Last.fm top album", 
                  "Couldn't identify this artist on MusicBrainz, so Lidarr can't add it" if status == "failed" else "Album chosen by Last.fm top album"))
conn.commit()
save_provider(conn, ProviderConfig(id=None, name="llama3.1:8b", kind="ollama", model="llama3.1:8b",
                                   base_url="http://192.168.1.20:11434", tool_mode="two_stage", is_default=True))
save_provider(conn, ProviderConfig(id=None, name="Anthropic (Claude)", kind="anthropic", model="claude-opus-5-5"))
save_provider(conn, ProviderConfig(id=None, name="OpenAI / OpenAI-compatible", kind="openai_compat",
                                   model="gpt-image-2.5-flare", base_url="https://api.openai.com/v1"))
keys = [str(1000 + i) for i in range(19)]
script = ScriptedProvider([tools(call("add_to_draft", plex_keys=keys)),
                           tools(call("finish", title="Road Trip Mix", reasoning="A mix of upbeat and mellow tracks to keep the energy level consistent throughout a long car drive."))])
gid = GenerationManager().start(conn, "A mix of upbeat and mellow tracks to keep the energy level consistent throughout a long car drive.",
                                background=False, inject={"provider": script}, constraints=Constraints(target_minutes=60), mode="tools")
draft_id = get_generation(conn, gid)["draft_id"]
buf = io.BytesIO(); Image.new("RGB", (1024, 1024), (200, 120, 40)).save(buf, "JPEG")
cid = covers.store(conn, buf.getvalue(), kind="ai_image", draft_id=draft_id, model="gpt-image-2.5-flare", cost=0.014)
covers.set_cover(conn, cid, draft_id=draft_id)
register_managed(conn, "901", "Road Trip Mix", 19, draft_id=draft_id)
conn.execute("INSERT INTO pushes(draft_id, plex_playlist_key, title, action, track_count, pushed_at) VALUES(?, '901', 'Road Trip Mix', 'created', 19, datetime('now'))", (draft_id,))
conn.commit()
rid = recipes.save_recipe(conn, {"name": "Weekly Alt-Rock Refresh With A Long Name", "prompt": "energetic alt rock " * 8,
                                 "provider_id": 2, "mode": None, "constraints": Constraints.from_form({"target_minutes": 90}),
                                 "schedule_cron": "0 6 * * 1", "enabled": True, "avoid_repeats": True,
                                 "cover_mode": "renew", "cover_prompt": None, "cover_provider_id": 3, "cover_id": cid})
conn.close()

# ------------------------------------------------------------------ serve
import uvicorn  # noqa: E402

from setlistarr.web.app import create_app  # noqa: E402

server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=18799, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.1)

PAGES = ["/", "/discover", "/settings/discover", "/library?playlist=902", "/generate", f"/generate/{gid}", "/library", "/library/artists", "/stats", "/history", "/recipes",
         "/recipes/new", f"/recipes/{rid}", "/settings", "/settings/providers", "/settings/providers/3", "/system", "/login"]
WIDTHS = [1440, 1280, 1100, 1000, 900, 800, 760, 600, 390]

DETECT = r"""
() => {
  const out = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw + 1)
    out.push({page: true, msg: `page scrolls sideways: ${document.documentElement.scrollWidth}px > ${vw}px`});
  const desc = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.classList.length) s += '.' + [...el.classList].join('.');
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    return t ? `${s} "${t}"` : s;
  };
  const scrolls = el => { for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll') return p; } return null; };
  const seen = new Set();
  for (const box of document.querySelectorAll('.card, .tile, .toolbar, .sidebar, .auth-card')) {
    const br = box.getBoundingClientRect();
    if (!br.width) continue;
    for (const el of box.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).position === 'fixed') continue;
      const sc = scrolls(el);
      if (sc && box.contains(sc) && sc !== box) continue;   // intentionally scrollable region
      const over = Math.max(r.right - br.right, br.left - r.left);
      if (over > 2) {
        const key = desc(box).split(' ')[0] + ' > ' + desc(el);
        if (seen.has(key)) continue; seen.add(key);
        // only report the outermost offender
        let parentOff = false;
        for (let p = el.parentElement; p && p !== box; p = p.parentElement) {
          const pr = p.getBoundingClientRect(); if (Math.max(pr.right - br.right, br.left - pr.left) > 2) { parentOff = true; break; }
        }
        if (!parentOff) out.push({msg: `${desc(box).split(' ')[0]} > ${desc(el)} sticks out ${Math.round(over)}px`});
      }
    }
  }
  for (const th of document.querySelectorAll('th')) {
    const r = th.getBoundingClientRect(), lh = parseFloat(getComputedStyle(th).lineHeight) || 18;
    const pad = parseFloat(getComputedStyle(th).paddingTop) + parseFloat(getComputedStyle(th).paddingBottom);
    if (r.width && th.innerText.trim() && r.height - pad > lh * 1.5)
      out.push({msg: 'column header wraps: ' + desc(th)});
  }
  const h = document.documentElement.scrollHeight;
  if (h > 6000) out.push({msg: 'page is ' + h + 'px tall (collapsed columns?)'});
  for (const w of document.querySelectorAll('.draft-editor .table-wrap'))
    if (w.scrollWidth > w.clientWidth + 1) out.push({msg: 'draft table needs sideways scroll (remove button off-screen?)'});
  if (vw >= 1000) for (const w of document.querySelectorAll('.table-wrap'))
    if (w.scrollWidth > w.clientWidth + 1) out.push({msg: 'table needs sideways scroll at desktop width: ' + desc(w.querySelector('table'))});
  return out;
}
"""

from playwright.sync_api import sync_playwright  # noqa: E402

shots = "--shots" in sys.argv
report = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    for w in WIDTHS:
        page = browser.new_page(viewport={"width": w, "height": 900})
        for path in PAGES:
            page.goto(f"http://127.0.0.1:18799{path}")
            page.wait_for_load_state("networkidle")
            if path.startswith("/generate/"):
                page.wait_for_selector(".draft-editor"); page.wait_for_selector(".cover-widget")
            if path == "/discover":  # open the song picker for the arrived artist
                page.click("text=Pick songs"); page.wait_for_selector(".song-picker")
            for d in page.query_selector_all("details"):  # open collapsibles so their content is checked too
                d.evaluate("el => el.open = true")
            page.wait_for_timeout(150)
            issues = page.evaluate(DETECT)
            if issues:
                report.setdefault(path, {})[w] = [i["msg"] for i in issues]
            if shots and w in (1440, 1100, 390) and os.makedirs(OUT, exist_ok=True) is None and path in (f"/generate/{gid}", "/recipes", "/settings/providers", f"/recipes/{rid}", "/library", "/library/artists", "/discover"):
                page.screenshot(path=f"{OUT}/shot-{w}-{path.strip('/').replace('/', '_') or 'home'}.png", full_page=True)
        page.close()
    browser.close()
server.should_exit = True

if not report:
    print("NO LAYOUT PROBLEMS FOUND at", WIDTHS)
for path, by_w in report.items():
    print(f"\n== {path}")
    for w, msgs in by_w.items():
        for m in msgs:
            print(f"  {w:>5}px  {m}")

sys.exit(1 if report else 0)
