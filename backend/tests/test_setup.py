"""First-run setup wizard: GET /api/setup/status, POST /api/setup/bootstrap.

Both routes are unauthenticated by design (see app/routers/setup.py's module
docstring) -- these tests hit them directly via TestClient with the in-memory
SQLite `wire_db_session` override (tests/conftest.py) standing in for admin-db.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(monkeypatch) -> TestClient:
    from app import security
    from app.main import app

    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)
    # set_session_cookie() marks the cookie `secure=settings.cookie_secure`;
    # TestClient talks plain http, so a secure cookie would silently never be
    # replayed on the follow-up request below. False mirrors local (non-TLS) dev,
    # which is what `BACKEND_PUBLIC_URL=http://...` produces (config._cookie_secure).
    monkeypatch.setattr(security.settings, "cookie_secure", False, raising=False)
    return TestClient(app)


def test_status_reports_needs_setup_when_no_users(monkeypatch, wire_db_session):
    client = _client(monkeypatch)
    resp = client.get("/api/setup/status")
    assert resp.status_code == 200
    assert resp.json() == {"needs_setup": True}


async def test_status_reports_setup_complete_once_a_user_exists(monkeypatch, wire_db_session):
    from app.db.models import User as UserRow

    wire_db_session.add(UserRow(username="someone", provider="credentials"))
    await wire_db_session.commit()

    client = _client(monkeypatch)
    resp = client.get("/api/setup/status")
    assert resp.json() == {"needs_setup": False}


def test_bootstrap_creates_first_admin_and_logs_in(monkeypatch, wire_db_session):
    client = _client(monkeypatch)
    resp = client.post("/api/setup/bootstrap", json={"username": "alice", "password": "s3cret-enough"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] == {"identifier": "alice", "role": "admin", "provider": "credentials"}
    assert resp.cookies.get("prometheus_session")

    # The session cookie from bootstrap is enough to hit an authenticated route.
    assert client.get("/api/auth/me").status_code == 200


async def test_bootstrap_password_is_hashed_not_stored_plaintext(monkeypatch, wire_db_session):
    from sqlalchemy import select

    from app.db.models import User as UserRow

    client = _client(monkeypatch)
    client.post("/api/setup/bootstrap", json={"username": "alice", "password": "s3cret-enough"})

    row = await wire_db_session.scalar(select(UserRow).where(UserRow.username == "alice"))
    assert row is not None
    assert row.password_hash != "s3cret-enough"
    assert row.password_hash.startswith("$2b$")


def test_bootstrap_rejects_once_a_user_already_exists(monkeypatch, wire_db_session):
    client = _client(monkeypatch)
    first = client.post("/api/setup/bootstrap", json={"username": "alice", "password": "s3cret-enough"})
    assert first.status_code == 200

    second = client.post("/api/setup/bootstrap", json={"username": "bob", "password": "another-secret"})
    assert second.status_code == 409


def test_bootstrap_requires_username_and_password(monkeypatch, wire_db_session):
    client = _client(monkeypatch)
    assert client.post("/api/setup/bootstrap", json={"username": "", "password": "x"}).status_code == 422
    assert client.post("/api/setup/bootstrap", json={"username": "alice", "password": ""}).status_code == 422
    assert client.post("/api/setup/bootstrap", json={"username": "  ", "password": "x"}).status_code == 422
