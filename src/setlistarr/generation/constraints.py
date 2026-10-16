"""Playlist constraints. Fields the user set in the form are locked; the model may fill the
rest (from the prompt) via set_constraints / two-stage criteria, but never override a lock."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

FIELDS = ("target_minutes", "max_per_artist", "exclude_artists", "exclude_tags", "year_min", "year_max")


@dataclass
class Constraints:
    target_minutes: float | None = None
    max_per_artist: int | None = None
    exclude_artists: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    year_min: int | None = None
    year_max: int | None = None
    # Tracks to keep out (a recipe's previous refresh). Not a form field; set by recipes.
    exclude_keys: list[str] = field(default_factory=list)
    locked: set[str] = field(default_factory=set)

    # defaults applied when neither the user nor the model set a value
    default_target_minutes: float = 60
    default_max_per_artist: int = 3

    @property
    def target(self) -> float:
        return float(self.target_minutes or self.default_target_minutes)

    @property
    def per_artist(self) -> int:
        return int(self.max_per_artist or self.default_max_per_artist)

    @property
    def tolerance_minutes(self) -> float:
        return max(3.0, self.target * 0.05)

    @property
    def window_ms(self) -> tuple[int, int]:
        lo = (self.target - self.tolerance_minutes) * 60_000
        hi = (self.target + self.tolerance_minutes) * 60_000
        return int(max(0, lo)), int(hi)

    @classmethod
    def from_form(cls, data: dict[str, Any], *, default_target: float = 60, default_cap: int = 3) -> "Constraints":
        """Build from user input; every non-empty field becomes locked."""
        c = cls(default_target_minutes=default_target, default_max_per_artist=default_cap)
        c.update(data, lock=True)
        return c

    def update(self, data: dict[str, Any], *, lock: bool = False) -> list[str]:
        """Apply values; unless lock=True, locked fields are skipped. Returns the fields changed."""
        changed = []
        for key in FIELDS:
            if key not in data:
                continue
            if not lock and key in self.locked:
                continue
            val = _clean(key, data[key])
            if val in (None, [], ""):
                continue
            setattr(self, key, val)
            changed.append(key)
            if lock:
                self.locked.add(key)
        return changed

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["locked"] = sorted(self.locked)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Constraints":
        d = dict(d or {})
        locked = set(d.pop("locked", []) or [])
        c = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        c.locked = locked
        return c

    def describe(self) -> str:
        parts = [f"length ~{self.target:g} min (±{self.tolerance_minutes:g})",
                 f"max {self.per_artist} tracks per artist"]
        if self.year_min or self.year_max:
            parts.append(f"years {self.year_min or '…'}-{self.year_max or '…'}")
        if self.exclude_artists:
            parts.append("excluding artists: " + ", ".join(self.exclude_artists))
        if self.exclude_tags:
            parts.append("excluding tags: " + ", ".join(self.exclude_tags))
        if self.exclude_keys:
            parts.append(f"avoiding the {len(self.exclude_keys)} tracks used in the previous refresh")
        return "; ".join(parts)


def _clean(key: str, val: Any) -> Any:
    if val is None:
        return None
    if key in ("exclude_artists", "exclude_tags"):
        if isinstance(val, str):
            val = [v for v in val.split(",")]
        out = [str(v).strip() for v in val if str(v).strip()]
        return [v.lower() for v in out] if key == "exclude_tags" else out
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
    try:
        if key == "target_minutes":
            f = float(val)
            return f if 5 <= f <= 24 * 60 else None
        if key == "max_per_artist":
            n = int(val)
            return n if n >= 1 else None
        if key in ("year_min", "year_max"):
            n = int(val)
            return n if 1000 <= n <= 2100 else None
    except (TypeError, ValueError):
        return None
    return val
