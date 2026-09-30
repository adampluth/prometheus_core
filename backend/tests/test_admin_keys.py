"""Admin signing-key management API: GET/POST /api/admin/signing-keys,
POST .../{id}/activate, POST .../{id}/retire.

Behind require_admin like admin_users.py. Most of this file is about the
"exactly one active key" invariant and the guards that keep mint_for_user()
from silently losing its DB key mid-rotation.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client_as(monkeypatch, *, role: str, sid: str) -> TestClient:
    from app import security
    from app.main import app

    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)
    token = security.issue_token(security.User(identifier=f"{role}-{sid}", role=role), sid=sid)
    client = TestClient(app)
    client.cookies.set("prometheus_session", token)
    return client


# --------------------------------------------------------------------------- #
# require_admin gate
# --------------------------------------------------------------------------- #


def test_unauthenticated_gets_401(wire_db_session):
    from app.main import app

    assert TestClient(app).get("/api/admin/signing-keys").status_code == 401


def test_non_admin_gets_403(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="user", sid="s-nonadmin")
    assert client.get("/api/admin/signing-keys").status_code == 403
    assert client.post("/api/admin/signing-keys").status_code == 403


# --------------------------------------------------------------------------- #
# generate / list
# --------------------------------------------------------------------------- #


def test_admin_can_generate_a_key_without_activating_it(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-gen")

    resp = client.post("/api/admin/signing-keys")
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_active"] is False
    assert body["retired_at"] is None
    assert "BEGIN PUBLIC KEY" in body["public_key_pem"]
    assert "PRIVATE KEY" not in str(body)  # private key material never returned


def test_generate_without_encryption_key_returns_an_actionable_error(monkeypatch, wire_db_session):
    """Regression: ADMIN_DB_ENCRYPTION_KEY unset used to 500 with a bare,
    JSON-less body (app.crypto.EncryptionError uncaught) -- app.main's
    exception_handler now turns it into a real, parseable detail message."""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_db_encryption_key", "")
    client = _client_as(monkeypatch, role="admin", sid="s-noenckey")

    resp = client.post("/api/admin/signing-keys")
    assert resp.status_code == 500
    assert "ADMIN_DB_ENCRYPTION_KEY" in resp.json()["detail"]


def test_list_shows_generated_keys(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-list")

    client.post("/api/admin/signing-keys")
    client.post("/api/admin/signing-keys")

    resp = client.get("/api/admin/signing-keys")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# --------------------------------------------------------------------------- #
# activate
# --------------------------------------------------------------------------- #


def test_activate_makes_exactly_one_key_active(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-activate")

    first = client.post("/api/admin/signing-keys").json()
    second = client.post("/api/admin/signing-keys").json()

    r1 = client.post(f"/api/admin/signing-keys/{first['id']}/activate")
    assert r1.status_code == 200
    assert r1.json()["is_active"] is True

    r2 = client.post(f"/api/admin/signing-keys/{second['id']}/activate")
    assert r2.status_code == 200
    assert r2.json()["is_active"] is True

    keys = client.get("/api/admin/signing-keys").json()
    active = [k for k in keys if k["is_active"]]
    assert len(active) == 1
    assert active[0]["id"] == second["id"]
    # The first key is deactivated but *not* retired by activating a replacement.
    first_now = next(k for k in keys if k["id"] == first["id"])
    assert first_now["is_active"] is False
    assert first_now["retired_at"] is None


def test_activate_unknown_key_404s(monkeypatch, wire_db_session):
    import uuid

    client = _client_as(monkeypatch, role="admin", sid="s-404")
    resp = client.post(f"/api/admin/signing-keys/{uuid.uuid4()}/activate")
    assert resp.status_code == 404


def test_cannot_reactivate_a_retired_key(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-reactivate")

    first = client.post("/api/admin/signing-keys").json()
    second = client.post("/api/admin/signing-keys").json()
    client.post(f"/api/admin/signing-keys/{first['id']}/activate")
    client.post(f"/api/admin/signing-keys/{second['id']}/activate")  # deactivates first
    client.post(f"/api/admin/signing-keys/{first['id']}/retire")

    resp = client.post(f"/api/admin/signing-keys/{first['id']}/activate")
    assert resp.status_code == 409


def test_activate_is_idempotent_on_the_already_active_key(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-idempotent")

    key = client.post("/api/admin/signing-keys").json()
    client.post(f"/api/admin/signing-keys/{key['id']}/activate")
    resp = client.post(f"/api/admin/signing-keys/{key['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# --------------------------------------------------------------------------- #
# retire
# --------------------------------------------------------------------------- #


def test_cannot_retire_the_active_key_with_no_replacement(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-retire-guard")

    key = client.post("/api/admin/signing-keys").json()
    client.post(f"/api/admin/signing-keys/{key['id']}/activate")

    resp = client.post(f"/api/admin/signing-keys/{key['id']}/retire")
    assert resp.status_code == 409


def test_can_retire_an_inactive_key(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-retire-ok")

    first = client.post("/api/admin/signing-keys").json()
    second = client.post("/api/admin/signing-keys").json()
    client.post(f"/api/admin/signing-keys/{first['id']}/activate")
    client.post(f"/api/admin/signing-keys/{second['id']}/activate")  # deactivates first

    resp = client.post(f"/api/admin/signing-keys/{first['id']}/retire")
    assert resp.status_code == 200
    assert resp.json()["retired_at"] is not None


def test_retiring_after_activating_a_replacement_still_serves_it_in_jwks(monkeypatch, wire_db_session):
    """Confirms the plan's explicit verify step: retiring the old key after
    rotation still lets in-flight tokens verify -- i.e. it stays in the JWKS."""
    client = _client_as(monkeypatch, role="admin", sid="s-retire-jwks")

    first = client.post("/api/admin/signing-keys").json()
    second = client.post("/api/admin/signing-keys").json()
    client.post(f"/api/admin/signing-keys/{first['id']}/activate")
    client.post(f"/api/admin/signing-keys/{second['id']}/activate")
    client.post(f"/api/admin/signing-keys/{first['id']}/retire")

    kids = {k["kid"] for k in client.get("/api/admin/signing-keys").json()}
    jwks_kids = {jwk["kid"] for jwk in client.get("/.well-known/jwks.json").json()["keys"]}
    assert kids == jwks_kids  # both the active and the retired key are still served


# --------------------------------------------------------------------------- #
# activate/retire invalidate the mint_for_user() cache
# --------------------------------------------------------------------------- #


def test_activate_invalidates_the_minting_cache(monkeypatch, wire_db_session):
    from app import signing_keys

    signing_keys.set_cached_active_key(("stale-kid", "stale-pem"))

    client = _client_as(monkeypatch, role="admin", sid="s-cache")
    key = client.post("/api/admin/signing-keys").json()
    client.post(f"/api/admin/signing-keys/{key['id']}/activate")

    is_fresh, _ = signing_keys.cached_active_key()
    assert is_fresh is False
