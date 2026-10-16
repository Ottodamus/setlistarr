"""finalize(): the last gate every generation passes through, whatever mode produced it.

Re-validates the model's ordered picks from scratch against the live library and the
constraints, trims to the target window, and backfills from tracks the model itself was
shown (its candidate pool) - never from random library tracks. Every change is reported.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .constraints import Constraints
from .draft import DraftState


@dataclass
class FinalDraft:
    tracks: list[dict[str, Any]]
    adjustments: list[str] = field(default_factory=list)

    @property
    def total_ms(self) -> int:
        return sum(t["duration_ms"] or 0 for t in self.tracks)


def finalize(
    conn: sqlite3.Connection,
    picks: list[str],
    constraints: Constraints,
    pool: list[str],
    *,
    sources: dict[str, str] | None = None,
    keep: set[str] | None = None,
) -> FinalDraft:
    """keep: user-locked keys - always included (if still in the library), never trimmed."""
    sources = sources or {}
    keep = {str(k) for k in keep or ()}
    adjustments: list[str] = []
    ds = DraftState(conn, constraints, enforce_duration=False)
    ds.locked = keep
    picks = list(picks) + [k for k in keep if k not in {str(p) for p in picks}]

    picks = [str(k) for k in picks]
    dropped: dict[str, list[str]] = {}
    # Kept tracks go in first so they count toward caps before any model pick is judged...
    for key in picks:
        if key in keep:
            ds.add([key], source="user", force=True)
    for key in picks:
        if key in keep:
            continue
        res = ds.add([key], source=sources.get(key, "model"))
        for r in res["rejected"]:
            dropped.setdefault(r["reason"], []).append(r["plex_key"])
    # ...then restore the intended play order.
    order = {k: i for i, k in enumerate(picks)}
    ds.tracks.sort(key=lambda t: order[t["plex_key"]])
    for reason, keys in dropped.items():
        if reason.startswith("not a track"):
            adjustments.append(f"Dropped {len(keys)} track id(s) not in your library: {', '.join(keys[:5])}")
        else:
            adjustments.append(f"Dropped {len(keys)} track(s): {reason}")

    lo, hi = constraints.window_ms
    trimmed = 0
    while ds.total_ms > hi:
        removable = [i for i, t in enumerate(ds.tracks) if t["plex_key"] not in keep]
        if not removable:
            break
        ds.tracks.pop(removable[-1])
        trimmed += 1
    if trimmed:
        adjustments.append(f"Trimmed {trimmed} track(s) from the end to fit ~{constraints.target:g} min")

    if ds.total_ms < lo:
        ds.enforce_duration = True
        added = 0
        for key in pool:
            if ds.total_ms >= lo:
                break
            if key in ds.keys:
                continue
            if ds.add([key], source="backfill")["accepted"]:
                added += 1
        if added:
            adjustments.append(f"Added {added} track(s) from the model's own search results to reach the target length")
        if ds.total_ms < lo:
            short = (lo - ds.total_ms) / 60000
            adjustments.append(
                f"Still {short:.1f} min short of the target - not enough matching tracks were found"
            )

    return FinalDraft(tracks=ds.tracks, adjustments=adjustments)
