import pytest
from fastapi.testclient import TestClient

from setlistarr.services.jobs import recent_jobs, run_job
from setlistarr.services.library import do_sync
from setlistarr.web.app import create_app

from .fakes.fake_plex import TOKEN


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_health_unconfigured_is_200_degraded(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded" and body["plex"]["status"] == "unconfigured"
    assert body["db"]["status"] == "ok"


def test_pages_render_empty(client):
    for path in ("/library", "/library/artists", "/stats", "/settings", "/system", "/generate"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/").headers.get("location") is None  # followed redirect -> /library
    assert client.get("/nope").status_code == 404


def test_settings_test_connection_saves_and_loads_sections(client, conn, fake_plex):
    r = client.post("/settings/plex/test", data={"plex_url": fake_plex.url + "/", "plex_token": TOKEN})
    assert "Connected to <b>FakePlex</b>" in r.text and "Music" in r.text
    assert conn.execute("SELECT value FROM settings WHERE key='plex_url'").fetchone()[0] == fake_plex.url

    r = client.post("/settings/plex/test", data={"plex_url": fake_plex.url, "plex_token": "bad"})
    assert "rejected the token" in r.text


def test_secret_left_blank_is_kept(client, conn, configured):
    client.post("/settings", data={"plex_url": "http://x:32400", "plex_token": "", "sync_interval_minutes": "15"})
    s = configured
    assert s.get("plex_token") == TOKEN and s.get("plex_url") == "http://x:32400"
    assert s.get("sync_interval_minutes") == 15 and s.get("enrich_track_tags") is False  # unchecked box


def test_env_pinned_setting_wins(monkeypatch, conn, configured):
    monkeypatch.setenv("PLEX_URL", "http://from-env:32400")
    assert configured.get("plex_url") == "http://from-env:32400" and configured.source("plex_url") == "env"


def test_sync_job_then_browse(client, conn, configured):
    run_job("sync-full", "test", lambda c: do_sync(c, full=True))
    job = recent_jobs(conn)[0]
    assert job["status"] == "ok", job

    r = client.get("/library?q=pixies&sort=title")
    assert "Debaser" in r.text and "Alison" not in r.text
    r = client.get("/library?tag=shoegaze", headers={"HX-Request": "true"})
    assert "<html" not in r.text and "Alison" in r.text
    assert "Slowdive" in client.get("/library/artists").text
    assert "Shoegaze".lower() in client.get("/stats").text


def test_job_failure_is_recorded_not_raised(conn):
    run_job("sync", "test", lambda c: do_sync(c, full=False))  # Plex not configured
    job = recent_jobs(conn)[0]
    assert job["status"] == "error" and "not configured" in job["error"]


def test_trigger_requires_plex(client):
    r = client.post("/system/jobs/sync")
    assert "Configure Plex" in r.text


def test_empty_env_values_from_compose_are_treated_as_unset(monkeypatch, conn):
    """docker-compose `${VAR:-}` passes empty strings; none of them may break startup or pin a setting."""
    from setlistarr import config
    from setlistarr.config import Settings

    for var in ("LOG_LEVEL", "LOG_FORMAT", "SETLISTARR_HOST", "SETLISTARR_PORT", "PLEX_URL", "PLEX_TOKEN",
                "ADMIN_USERNAME", "ADMIN_PASSWORD", "SETLISTARR_SECRET_KEY"):
        monkeypatch.setenv(var, "")
    config.bootstrap.cache_clear()
    b = config.bootstrap()
    assert (b.log_level, b.port, b.host) == ("INFO", 8787, "0.0.0.0") and b.secret_key
    assert Settings(conn).source("plex_url") != "env"
    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200
