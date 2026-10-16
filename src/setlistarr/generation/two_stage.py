"""Two-stage mode, for models with weak tool calling:

  1. the model turns the request into JSON criteria (tags from the real vocabulary),
  2. we query SQLite for a diversified shortlist,
  3. the model picks and orders by candidate *number* - it can't name a track that isn't listed.
"""

from __future__ import annotations

import difflib
import os
import math
from typing import Any

from ..core.tools import ToolContext, list_facets, list_genres, sample_tracks, search_tracks
from ..llm.base import ProviderError, extract_json
from .common import GenContext, ModeResult
from .constraints import Constraints
from .draft import norm_artist
from .prompts import CRITERIA_SCHEMA, ORDER_SCHEMA, STAGE1_SYSTEM, STAGE1_USER, STAGE2_SYSTEM, STAGE2_USER

AVG_TRACK_SEC = 240
MIN_TRACK_SEC = 90  # skips skits, intros and interludes in shortlists


def split_genre_mood(conn, tags: list[str]) -> tuple[list[str], list[str]]:
    """Genre/style tags (Plex genre/style, MusicBrainz, Last.fm) filter a shortlist; Plex mood
    tags only rank it. ORing both made 'energetic' pop qualify for an alt-rock request."""
    if not tags:
        return [], []
    rows = conn.execute(
        f"SELECT tag, SUM(source = 'plex-mood') AS mood, SUM(source != 'plex-mood') AS other "
        f"FROM track_tags WHERE tag IN ({','.join('?' * len(tags))}) GROUP BY tag",
        tags,
    ).fetchall()
    kind = {r["tag"]: ("mood" if r["mood"] and not r["other"] else "genre") for r in rows}
    genres = [t for t in tags if kind.get(t) == "genre"]
    moods = [t for t in tags if kind.get(t) == "mood"]
    return genres, moods


def map_tags(wanted: list[str], vocab: dict[str, int]) -> tuple[list[str], list[str]]:
    """Map model-supplied tags onto the library vocabulary. Returns (mapped, dropped)."""
    mapped, dropped = [], []
    names = list(vocab)
    for raw in wanted or []:
        t = str(raw).strip().lower()
        if not t:
            continue
        if t in vocab:
            hit = [t]
        else:
            hit = difflib.get_close_matches(t, names, n=1, cutoff=0.85)
            if not hit:  # inflections: shoegazing ~ shoegaze (but never punk ~ funk)
                pre = [n for n in names
                       if len(p := os.path.commonprefix([t, n])) >= 6 and len(p) >= 0.7 * min(len(t), len(n))]
                hit = sorted(pre, key=lambda n: -vocab[n])[:1]
            if not hit:
                hit = sorted((n for n in names if t in n or n in t), key=lambda n: -vocab[n])[:2]
        if hit:
            mapped += [h for h in hit if h not in mapped]
        else:
            dropped.append(t)
    return mapped, dropped


def _ask_json(gx: GenContext, system: str, user: str, schema: dict, *, retry_hint: str) -> dict | None:
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    for attempt in range(2):
        resp = gx.chat(system=system, messages=messages, json_schema=schema, max_tokens=4000)
        try:
            data = extract_json(resp.text)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        gx.emit("warn", "model reply wasn't valid JSON" + (" - retrying" if attempt == 0 else ""))
        messages += [resp.as_message(), {"role": "user", "content": retry_hint}]
    return None


def _filters(conn, crit: dict, c: Constraints) -> dict[str, Any]:
    genres, moods = split_genre_mood(conn, crit.get("any_tags") or [])
    if not genres:  # mood-only request ("something chill"): moods have to do the filtering
        genres, moods = moods, []
    f: dict[str, Any] = {
        "any_tags": genres or None,
        "boost_tags": moods or None,
        "duration_min_sec": MIN_TRACK_SEC,
        "all_tags": crit.get("all_tags") or None,
        "exclude_tags": sorted(set(crit.get("exclude_tags") or []) | set(c.exclude_tags)) or None,
        "artists": crit.get("artists") or None,
        "exclude_artists": sorted(set(crit.get("exclude_artists") or []) | set(c.exclude_artists)) or None,
        "year_min": c.year_min,
        "year_max": c.year_max,
    }
    played = crit.get("played")
    if played == "unplayed":
        f["never_played"] = True
    rating = crit.get("min_rating")
    if played == "favorites":
        rating = max(float(rating or 0), 8.0)
    if rating:
        f["min_user_rating"] = float(rating)
    return {k: v for k, v in f.items() if v not in (None, [], False)}


def build_shortlist(
    lib: ToolContext, filters: dict[str, Any], c: Constraints, size: int, need: int
) -> tuple[list[dict], list[str]]:
    """Relevance-ranked rows plus a random sample, capped per artist. Relaxes filters
    (strictest first) when too few tracks match. Returns (rows, notes)."""
    attempts = [
        ("", filters),
        ("dropped required tags and named artists",
         {k: v for k, v in filters.items() if k not in ("all_tags", "artists")}),
        ("also dropped the year range and rating filters",
         {k: v for k, v in filters.items()
          if k not in ("all_tags", "artists", "year_min", "year_max", "min_user_rating", "never_played")}),
    ]
    cap = max(4, 2 * c.per_artist)
    rows: list[dict] = []
    notes: list[str] = []
    for note, f in attempts:
        ranked = search_tracks(lib, **f, sort="relevance", limit=200)["tracks"]
        sampled = sample_tracks(lib, n=120, **{k: v for k, v in f.items() if k != "boost_tags"})["tracks"]
        seen, per_artist, rows = set(), {}, []
        for r in ranked[: size * 2 // 3] + sampled + ranked[size * 2 // 3 :]:
            a = norm_artist(r["artist"] or "")
            if r["plex_key"] in seen or per_artist.get(a, 0) >= cap:
                continue
            seen.add(r["plex_key"])
            per_artist[a] = per_artist.get(a, 0) + 1
            rows.append(r)
        if note:
            notes.append(f"Too few tracks matched, so the search {note}")
        if len(rows) >= need:
            break
    return rows[:size], notes


def run_two_stage(gx: GenContext, prompt: str, c: Constraints, seed: list[str] | None = None) -> ModeResult:
    from .draft import fetch_tracks

    lib = ToolContext(conn=gx.conn)
    seed_rows = [r for k in seed or [] if (r := fetch_tracks(gx.conn, [k]).get(k))]
    seed_ms = sum(r["duration_ms"] or 0 for r in seed_rows)
    facets = list_facets(lib, top=40)
    vocab = {t["tag"]: t["tracks"] for t in list_genres(lib, top=300)["tags"]}
    if not vocab and not facets["totals"]["tracks"]:
        raise ProviderError("The library is empty - run a sync first")

    # ---- stage 1: criteria ------------------------------------------------
    gx.emit("stage", "stage 1: turning the request into search criteria")
    tag_list = list(vocab)[:300]
    genre_tags, mood_tags = split_genre_mood(gx.conn, tag_list)
    user = STAGE1_USER.format(
        prompt=prompt,
        fixed=c.describe() if c.locked else "none",
        genres=", ".join(f"{t}: {vocab[t]}" for t in genre_tags[:180]),
        moods=", ".join(f"{t}: {vocab[t]}" for t in mood_tags[:80]),
        decades=", ".join(f"{d['decade']}s: {d['tracks']}" for d in facets["decades"] if d["decade"]),
    )
    crit = _ask_json(gx, STAGE1_SYSTEM, user, CRITERIA_SCHEMA,
                     retry_hint="Reply with only the JSON object described above.") or {}
    notes: list[str] = []
    for key in ("any_tags", "all_tags", "exclude_tags"):
        mapped, dropped = map_tags(crit.get(key) or [], vocab)
        crit[key] = mapped
        if dropped:
            notes.append(f"Ignored tags not in your library: {', '.join(dropped[:8])}")
    c.update({k: crit.get(k) for k in ("target_minutes", "max_per_artist", "year_min", "year_max",
                                       "exclude_artists", "exclude_tags")})
    gx.emit("constraints", f"criteria: tags={','.join(crit.get('any_tags') or []) or 'none'} · {c.describe()}")

    # ---- shortlist --------------------------------------------------------
    remaining_min = max(5.0, c.target - seed_ms / 60000)
    n_tracks = max(3, math.ceil(remaining_min * 60 / AVG_TRACK_SEC))
    size = max(40, min(200, n_tracks * 3))
    filters = _filters(gx.conn, crit, c)
    gx.emit("stage", f"filter tags: {', '.join(filters.get('any_tags') or []) or 'none'}"
                     f" · ranking boost: {', '.join(filters.get('boost_tags') or []) or 'none'}")
    skip = [r["plex_key"] for r in seed_rows] + list(c.exclude_keys)
    if skip:
        filters["exclude_keys"] = skip
    rows, relax_notes = build_shortlist(lib, filters, c, size, need=math.ceil(n_tracks * 1.5))
    notes += relax_notes
    if not rows:
        raise ProviderError("No tracks in the library match this request")
    gx.emit("stage", f"shortlist: {len(rows)} candidates")

    # ---- stage 2: pick + order -------------------------------------------
    gx.emit("stage", "stage 2: picking and ordering")
    lines = "\n".join(
        f"{i} | {r['artist']} - {r['title']} | {r['album'] or ''} | {r['year'] or ''} | "
        f"{r['duration_sec'] // 60}:{r['duration_sec'] % 60:02d}"
        for i, r in enumerate(rows, 1)
    )
    constraints_text = c.describe()
    if seed_rows:
        constraints_text += ("; the user already kept these tracks (they will play first, don't repeat them): "
                             + "; ".join(f"{r['artist']} - {r['title']}" for r in seed_rows))
    user2 = STAGE2_USER.format(prompt=prompt, constraints=constraints_text, n_tracks=n_tracks,
                               minutes=remaining_min, per_artist=c.per_artist, candidates=lines)
    out = _ask_json(gx, STAGE2_SYSTEM, user2, ORDER_SCHEMA,
                    retry_hint='Reply with only {"title": ..., "reasoning": ..., "order": [numbers]}.')
    keys = [r["plex_key"] for r in rows]
    picks: list[str] = []
    if out and isinstance(out.get("order"), list):
        bad = 0
        for n in out["order"]:
            try:
                i = int(n)
            except (TypeError, ValueError):
                bad += 1
                continue
            if 1 <= i <= len(keys):
                picks.append(keys[i - 1])
            else:
                bad += 1
        if bad:
            notes.append(f"Ignored {bad} pick(s) that weren't on the candidate list")
    if not picks:
        notes.append("The model's selection was unusable, so the ranked shortlist was used as-is")
        picks = keys
        out = out or {}
    return ModeResult(
        mode="two_stage", picks=[r["plex_key"] for r in seed_rows] + picks, pool=keys,
        title=(out.get("title") or "").strip() or None,
        reasoning=(out.get("reasoning") or "").strip() or None,
        notes=notes,
    )
