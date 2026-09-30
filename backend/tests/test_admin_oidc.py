"""Admin OIDC-provider management API: GET/POST /api/admin/oidc-providers,
PATCH/DELETE /api/admin/oidc-providers/{id}.

Behind require_admin like admin_users.py/admin_keys.py. Covers client_secret
write-only-ness, provider_id immutability/slug validation, and the registry
side-effects (create registers, delete unregisters, update needs neither --
see app/security.py::GenericOidcProvider's docstring).
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


_CREATE_PAYLOAD = {
    "provider_id": "okta",
    "display_name": "Okta",
    "issuer_url": "https://example.okta.com",
    "client_id": "client-abc",
    "client_secret": "s3cret-client-secret",
    "scopes": "openid profile email",
    "default_role": "user",
    "is_enabled": True,
}


# --------------------------------------------------------------------------- #
# require_admin gate
# --------------------------------------------------------------------------- #


def test_unauthenticated_gets_401(wire_db_session):
    from app.main import app

    assert TestClient(app).get("/api/admin/oidc-providers").status_code == 401


def test_non_admin_gets_403(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="user", sid="s-nonadmin")
    assert client.get("/api/admin/oidc-providers").status_code == 403
    assert client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD).status_code == 403


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


def test_admin_can_create_a_provider(monkeypatch, wire_db_session):
    from app import security

    client = _client_as(monkeypatch, role="admin", sid="s-create")
    resp = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert body["provider_id"] == "okta"
    assert "client_secret" not in body
    assert "client_secret_encrypted" not in body

    # Registered immediately -- no restart / rebuild-on-schedule needed.
    provider = security.get_provider("okta")
    assert provider is not None
    assert provider.name == "okta"


def test_create_without_encryption_key_returns_an_actionable_error(monkeypatch, wire_db_session):
    """Same regression as test_admin_keys.py's equivalent -- client_secret
    encryption hits the same app.crypto.EncryptionError path."""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_db_encryption_key", "")
    client = _client_as(monkeypatch, role="admin", sid="s-noenckey")

    resp = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD)
    assert resp.status_code == 500
    assert "ADMIN_DB_ENCRYPTION_KEY" in resp.json()["detail"]


def test_create_rejects_bad_slug(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-badslug")
    payload = dict(_CREATE_PAYLOAD, provider_id="Not A Slug!")
    resp = client.post("/api/admin/oidc-providers", json=payload)
    assert resp.status_code == 422


def test_create_rejects_missing_secret(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-nosecret")
    payload = dict(_CREATE_PAYLOAD, client_secret="")
    resp = client.post("/api/admin/oidc-providers", json=payload)
    assert resp.status_code == 422


def test_create_rejects_unknown_default_role(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-badrole")
    payload = dict(_CREATE_PAYLOAD, default_role="superuser")
    resp = client.post("/api/admin/oidc-providers", json=payload)
    assert resp.status_code == 422


def test_create_rejects_duplicate_provider_id(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-dup")
    assert client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD).status_code == 201
    resp = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD)
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_never_includes_the_secret(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-list")
    client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD)

    resp = client.get("/api/admin/oidc-providers")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert "s3cret-client-secret" not in str(body)


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #


def test_update_edits_fields_and_keeps_secret_if_omitted(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-update")
    created = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD).json()

    resp = client.patch(f"/api/admin/oidc-providers/{created['id']}", json={"display_name": "Okta (renamed)"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Okta (renamed)"
    assert resp.json()["provider_id"] == "okta"  # unchanged


def test_update_can_disable_a_provider(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, role="admin", sid="s-disable")
    created = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD).json()

    resp = client.patch(f"/api/admin/oidc-providers/{created['id']}", json={"is_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["is_enabled"] is False


def test_update_unknown_provider_404s(monkeypatch, wire_db_session):
    import uuid

    client = _client_as(monkeypatch, role="admin", sid="s-404")
    resp = client.patch(f"/api/admin/oidc-providers/{uuid.uuid4()}", json={"display_name": "x"})
    assert resp.status_code == 404


async def test_update_client_secret_is_re_encrypted(monkeypatch, wire_db_session):
    from sqlalchemy import select

    from app.crypto import decrypt
    from app.db.models import OidcProvider

    client = _client_as(monkeypatch, role="admin", sid="s-secret")
    created = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD).json()

    client.patch(f"/api/admin/oidc-providers/{created['id']}", json={"client_secret": "brand-new-secret"})

    row = await wire_db_session.scalar(select(OidcProvider).where(OidcProvider.provider_id == "okta"))
    assert decrypt(row.client_secret_encrypted) == "brand-new-secret"


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_delete_removes_the_row_and_unregisters(monkeypatch, wire_db_session):
    from app import security

    client = _client_as(monkeypatch, role="admin", sid="s-delete")
    created = client.post("/api/admin/oidc-providers", json=_CREATE_PAYLOAD).json()
    assert security.get_provider("okta") is not None

    resp = client.delete(f"/api/admin/oidc-providers/{created['id']}")
    assert resp.status_code == 204
    assert security.get_provider("okta") is None
    assert client.get("/api/admin/oidc-providers").json() == []
