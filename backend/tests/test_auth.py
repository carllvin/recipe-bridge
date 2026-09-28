"""Optional password protection (APP_PASSWORD)."""
import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.config import settings


@pytest.fixture
def client():
    auth._failures.clear()
    return TestClient(main.app)


def test_off_without_password(client):
    assert client.get("/api/config").status_code == 200


def test_everything_needs_signing_in(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "geheim")
    assert client.get("/api/config").status_code == 401
    page = client.get("/?import=https%3A%2F%2Fx.example%2Fr", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=%2F%3Fimport%3D")
    assert client.post("/share", data={"text": "x"}, follow_redirects=False).status_code == 303
    # open: health check, login page and what it needs
    assert client.get("/api/ping").json()["ok"]
    assert client.get("/login").status_code == 200
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/icons/icon-192.png").status_code == 200


def test_sign_in_and_out(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "geheim")
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    assert client.post("/api/login", json={"password": "falsch"}).status_code == 401
    ok = client.post("/api/login", json={"password": "geheim"})
    assert ok.status_code == 200 and auth.COOKIE in ok.cookies
    assert client.get("/api/config").json()["auth_enabled"] is True
    # a new password signs every device out
    monkeypatch.setattr(settings, "app_password", "neu")
    assert client.get("/api/config").status_code == 401
    monkeypatch.setattr(settings, "app_password", "geheim")
    client.get("/logout")
    assert client.get("/api/config").status_code == 401


def test_guessing_is_locked(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "geheim")
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/login", json={"password": "x"}).status_code == 401
    # locked - even the right password has to wait
    assert client.post("/api/login", json={"password": "geheim"}).status_code == 429


async def _no_sleep(_):
    return None
