"""Configuration.

Two layers:
  * Bootstrap (env only, read once): where /config lives, secret key, logging, bind address.
  * Settings (settings table, editable in the UI). Each setting may be pinned by an env var;
    an env value always wins and the UI shows the field as read-only.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Bootstrap:
    config_dir: Path
    db_path: Path
    secret_key: str
    log_level: str
    log_format: str  # 'json' | 'console'
    host: str
    port: int
    cookie_secure: bool


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def _load_secret_key(config_dir: Path) -> str:
    env = os.environ.get("SETLISTARR_SECRET_KEY", "").strip()
    if env:
        return env
    path = config_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    return key


@lru_cache(maxsize=1)
def bootstrap() -> Bootstrap:
    default_dir = "/config" if Path("/config").is_dir() else "./config"
    config_dir = Path(os.environ.get("SETLISTARR_CONFIG_DIR") or default_dir).expanduser().resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(os.environ.get("SETLISTARR_DB") or config_dir / "setlistarr.db").expanduser()
    return Bootstrap(
        config_dir=config_dir,
        db_path=db_path,
        secret_key=_load_secret_key(config_dir),
        log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        log_format=(os.environ.get("LOG_FORMAT") or "json").lower(),
        host=os.environ.get("SETLISTARR_HOST") or "0.0.0.0",
        port=int(os.environ.get("SETLISTARR_PORT") or 8787),
        cookie_secure=_env_bool("SETLISTARR_COOKIE_SECURE", False),
    )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: type = str
    default: Any = None
    env: str | None = None
    secret: bool = False
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (value, label); anything else falls back to default


SETTING_DEFS: dict[str, SettingDef] = {
    d.key: d
    for d in [
        SettingDef("ui_theme", "Theme", str, "dark",
                   choices=(("dark", "Dark"), ("light", "Light"), ("midnight", "Midnight"), ("synthwave", "Synthwave"))),
        SettingDef("plex_url", "Plex server URL", env="PLEX_URL",
                   help="e.g. http://192.168.1.10:32400 - the server itself, not app.plex.tv"),
        SettingDef("plex_token", "Plex token", env="PLEX_TOKEN", secret=True),
        SettingDef("plex_timeout_sec", "Plex request timeout (s)", int, 30),
        SettingDef("sync_interval_minutes", "Incremental sync every (min)", int, 60,
                   help="0 disables scheduled incremental sync"),
        SettingDef("full_sync_cron", "Full sync schedule (cron)", str, "30 3 * * *",
                   help="Catches deletions and anything incremental sync misses. Empty disables."),
        SettingDef("lastfm_api_key", "Last.fm API key", env="LASTFM_API_KEY", secret=True),
        SettingDef("musicbrainz_contact", "MusicBrainz contact", env="MUSICBRAINZ_CONTACT",
                   default="", help="Email or URL; MusicBrainz requires it in the User-Agent"),
        SettingDef("enrich_after_full_sync", "Enrich after full sync", bool, True),
        SettingDef("enrich_track_tags", "Fetch per-track Last.fm tags", bool, True,
                   help="One request per track on first run - slow on large libraries"),
        SettingDef("default_target_minutes", "Default playlist length (min)", int, 60,
                   help="Used when neither the form nor the prompt says how long"),
        SettingDef("default_max_per_artist", "Default max tracks per artist", int, 3),
        # --- Discover ------------------------------------------------------------------
        SettingDef("discover_source", "Recommendation source", str, "lastfm",
                   choices=(("lastfm", "Last.fm similar artists"), ("listenbrainz", "ListenBrainz recommendations")),
                   help="Last.fm: artists similar to the ones you play most (needs the Last.fm key under Settings). "
                        "ListenBrainz: personal recommendations from your listening history (needs a few weeks of plays)."),
        SettingDef("discover_cadence", "Discover playlist", str, "off",
                   choices=(("off", "Off (manual only)"), ("weekly", "Discover Weekly - Mondays 6:00"),
                            ("monthly", "Discover Monthly - the 1st, 6:00"))),
        SettingDef("discover_auto_approve", "Send new picks to Lidarr automatically", bool, False,
                   help="Off: nothing is downloaded until you approve it on the Discover page."),
        SettingDef("discover_per_run", "New artists per run", int, 8),
        SettingDef("discover_minutes", "Playlist length (min)", int, 60),
        SettingDef("discover_window_weeks", "Include arrivals from the last (weeks)", int, 4),
        SettingDef("discover_playlist_title", "Playlist title", str, "",
                   help="Empty = Discover Weekly / Discover Monthly"),
        SettingDef("discover_cover_mode", "Playlist cover", str, "none",
                   choices=(("none", "Don't touch the Plex cover"), ("keep", "Keep one cover"),
                            ("renew", "New AI cover every run"))),
        SettingDef("discover_cover_provider_id", "Cover provider", int, None),
        SettingDef("lidarr_url", "Lidarr URL", env="LIDARR_URL", help="e.g. http://192.168.1.10:8686"),
        SettingDef("lidarr_api_key", "Lidarr API key", env="LIDARR_API_KEY", secret=True,
                   help="Lidarr -> Settings -> General -> Security -> API Key"),
        SettingDef("lidarr_root_folder", "Root folder", str, ""),
        SettingDef("lidarr_quality_profile_id", "Quality profile", int, None),
        SettingDef("lidarr_metadata_profile_id", "Metadata profile", int, None),
        SettingDef("listenbrainz_token", "ListenBrainz token", env="LISTENBRAINZ_TOKEN", secret=True,
                   help="listenbrainz.org -> your profile -> Settings -> User token"),
        SettingDef("listenbrainz_user", "ListenBrainz user", str, "", help="Filled in when the token is tested"),
        SettingDef("scrobble_enabled", "Send my Plex plays to ListenBrainz", bool, False),
        SettingDef("scrobble_plex_account_id", "Plex account to send", int, 1,
                   help="Only this Plex account's plays are sent (1 = the server owner)"),
    ]
}


def _coerce(defn: SettingDef, raw: str | None) -> Any:
    if raw is None or raw == "":
        return defn.default
    if defn.choices and raw not in {v for v, _ in defn.choices}:
        return defn.default
    if defn.kind is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if defn.kind is int:
        try:
            return int(raw)
        except ValueError:
            return defn.default
    return raw


class Settings:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _db_value(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def source(self, key: str) -> str:
        defn = SETTING_DEFS[key]
        if defn.env and os.environ.get(defn.env, "").strip():
            return "env"
        if self._db_value(key) not in (None, ""):
            return "db"
        return "default"

    def get(self, key: str) -> Any:
        defn = SETTING_DEFS[key]
        if defn.env:
            env = os.environ.get(defn.env, "").strip()
            if env:
                return _coerce(defn, env)
        return _coerce(defn, self._db_value(key))

    def set(self, key: str, value: Any) -> None:
        if key not in SETTING_DEFS:
            raise KeyError(key)
        if isinstance(value, bool):
            value = "true" if value else "false"
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, None if value is None else str(value)),
        )
        self.conn.commit()

    def plex_configured(self) -> bool:
        return bool(self.get("plex_url") and self.get("plex_token"))
