import pytest
from fastapi.testclient import TestClient

from setlistarr.web import auth
from setlistarr.web.app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SETLISTARR_AUTH_DISABLED")
    with TestClient(create_app(), base_url="http://setlistarr.lan") as c:
        yield c


def test_password_hashing():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h) and not auth.verify_password("x", "garbage")


def test_first_run_setup_then_login_flow(client):
    assert client.get("/health").status_code == 200  # always public
    r = client.get("/library", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"

    r = client.post("/setup", data={"username": "admin", "password": "short", "password2": "short"})
    assert r.status_code == 400 and "at least 8" in r.text
    r = client.post("/setup", data={"username": "admin", "password": "longenough1", "password2": "longenough1"},
                    follow_redirects=False)
    assert r.status_code == 303 and client.get("/library").status_code == 200  # logged in by setup
    # setup is one-shot
    r = client.post("/setup", data={"username": "evil", "password": "takeover123", "password2": "takeover123"},
                    follow_redirects=False)
    assert r.headers["location"] == "/login"

    client.post("/logout")
    r = client.get("/stats?x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/stats%3Fx%3D1"
    r = client.get("/system", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/login")

    assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "longenough1", "next": "//evil.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"  # open-redirect blocked
    assert client.get("/stats").status_code == 200


def test_cross_origin_posts_blocked(client):
    client.post("/setup", data={"username": "admin", "password": "longenough1", "password2": "longenough1"})
    r = client.post("/settings", data={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/settings", data={"sync_interval_minutes": "30"}, headers={"Origin": "http://setlistarr.lan"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_env_pinned_admin(monkeypatch, conn):
    monkeypatch.delenv("SETLISTARR_AUTH_DISABLED")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "fromtheenv1")
    with TestClient(create_app()) as c:
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"
        c.post("/login", data={"username": "admin", "password": "fromtheenv1"})
        r = c.post("/settings/password", data={"current": "fromtheenv1", "new": "newpass123", "new2": "newpass123"})
        assert "set by ADMIN_PASSWORD" in r.text


def test_health_hides_details_until_logged_in(client):
    assert client.get("/health").json() == {"status": "degraded"}  # no Plex configured
    client.post("/setup", data={"username": "admin", "password": "longenough1", "password2": "longenough1"})
    body = client.get("/health").json()
    assert body["db"]["status"] == "ok" and "plex" in body
