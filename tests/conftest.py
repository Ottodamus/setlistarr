from __future__ import annotations

import pytest

from setlistarr import config
from setlistarr.config import Settings
from setlistarr.core.db import connect, migrate
from setlistarr.core.plex.client import PlexClient

from .fakes.fake_plex import TOKEN, FakePlex, seed_basic


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own /config dir and DB, with no env-pinned settings."""
    monkeypatch.setenv("SETLISTARR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("SETLISTARR_DB", raising=False)
    monkeypatch.setenv("SETLISTARR_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("SETLISTARR_AUTH_DISABLED", "1")  # test_auth.py turns it back on
    for var in ("ADMIN_USERNAME", "ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    for d in config.SETTING_DEFS.values():
        if d.env:
            monkeypatch.delenv(d.env, raising=False)
    config.bootstrap.cache_clear()
    yield
    config.bootstrap.cache_clear()


@pytest.fixture
def conn():
    c = connect(config.bootstrap().db_path)
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def fake_plex(httpserver):
    fp = seed_basic(FakePlex())
    fp.url = fp.install(httpserver)
    return fp


@pytest.fixture
def plex(fake_plex):
    with PlexClient(fake_plex.url, TOKEN) as client:
        yield client


@pytest.fixture
def configured(conn, fake_plex):
    s = Settings(conn)
    s.set("plex_url", fake_plex.url)
    s.set("plex_token", TOKEN)
    return s
