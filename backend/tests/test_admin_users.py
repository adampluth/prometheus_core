"""Admin user-management API: GET/POST/PATCH/DELETE /api/admin/users.

All behind require_admin (app/security.py) -- most of this file is about that
gate and the "never remove the last active admin" guard (app/routers/admin_users.py).
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client_as(monkeypatch, wire_db_session, *, role: str, sid: str) -> TestClient:
    from app import security
    from app.main import app

    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)
    token = security.issue_token(security.User(identifier=f"{role}-{sid}", role=role), sid=sid)
    client = TestClient(app)
    client.cookies.set("prometheus_session", token)
    return client


async def _seed_admin(wire_db_session, *, username: str = "root", role: str = "admin", is_active: bool = True):
    from app.db.models import User as UserRow
    from app.passwords import hash_password

    row = UserRow(
        username=username, password_hash=hash_password("x"), role=role, is_active=is_active, provider="credentials"
    )
    wire_db_session.add(row)
    await wire_db_session.commit()
    return row


# --------------------------------------------------------------------------- #
# require_admin gate
# --------------------------------------------------------------------------- #


def test_unauthenticated_gets_401(wire_db_session):
    from app.main import app

    assert TestClient(app).get("/api/admin/users").status_code == 401


def test_non_admin_gets_403(monkeypatch, wire_db_session):
    client = _client_as(monkeypatch, wire_db_session, role="user", sid="s-nonadmin")
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/users", json={"username": "x", "password": "y"}).status_code == 403


# --------------------------------------------------------------------------- #
# list / create
# --------------------------------------------------------------------------- #


async def test_admin_can_list_users(monkeypatch, wire_db_session):
    await _seed_admin(wire_db_session, username="alice")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-list")

    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()]
    assert "alice" in usernames


async def test_admin_can_create_local_user(monkeypatch, wire_db_session):
    await _seed_admin(wire_db_session)
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-create")

    resp = client.post("/api/admin/users", json={"username": "newbie", "password": "s3cret-enough", "role": "user"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "newbie"
    assert body["role"] == "user"
    assert body["provider"] == "credentials"
    assert body["is_active"] is True


async def test_create_rejects_duplicate_username(monkeypatch, wire_db_session):
    await _seed_admin(wire_db_session, username="dup")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-dup")

    resp = client.post("/api/admin/users", json={"username": "dup", "password": "s3cret-enough"})
    assert resp.status_code == 409


async def test_create_rejects_unknown_role(monkeypatch, wire_db_session):
    await _seed_admin(wire_db_session)
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-badrole")

    resp = client.post("/api/admin/users", json={"username": "x", "password": "y", "role": "superuser"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #


async def test_admin_can_promote_and_deactivate_another_user(monkeypatch, wire_db_session):
    await _seed_admin(wire_db_session, username="root-admin")
    target = await _seed_admin(wire_db_session, username="plain", role="user")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-promote")

    resp = client.patch(f"/api/admin/users/{target.id}", json={"role": "admin"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"

    resp2 = client.patch(f"/api/admin/users/{target.id}", json={"is_active": False})
    assert resp2.status_code == 200
    assert resp2.json()["is_active"] is False


async def test_update_password_actually_changes_it(monkeypatch, wire_db_session):
    from app import security

    await _seed_admin(wire_db_session)
    target = await _seed_admin(wire_db_session, username="haspw")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-pw")

    resp = client.patch(f"/api/admin/users/{target.id}", json={"password": "brand-new-password"})
    assert resp.status_code == 200

    provider = security.PasswordIdentityProvider()
    assert await provider.authenticate("haspw", "brand-new-password", wire_db_session) is not None
    assert await provider.authenticate("haspw", "x", wire_db_session) is None  # old password no longer works


async def test_update_unknown_user_404s(monkeypatch, wire_db_session):
    import uuid

    await _seed_admin(wire_db_session)
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-404")

    resp = client.patch(f"/api/admin/users/{uuid.uuid4()}", json={"role": "user"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# last-admin guard
# --------------------------------------------------------------------------- #


async def test_cannot_demote_the_last_active_admin(monkeypatch, wire_db_session):
    only_admin = await _seed_admin(wire_db_session, username="lonely-admin")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-lastadmin")

    resp = client.patch(f"/api/admin/users/{only_admin.id}", json={"role": "user"})
    assert resp.status_code == 409


async def test_cannot_deactivate_the_last_active_admin(monkeypatch, wire_db_session):
    only_admin = await _seed_admin(wire_db_session, username="lonely-admin-2")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-lastadmin2")

    resp = client.patch(f"/api/admin/users/{only_admin.id}", json={"is_active": False})
    assert resp.status_code == 409


async def test_can_demote_an_admin_when_another_remains(monkeypatch, wire_db_session):
    await _seed_admin(wire_db_session, username="admin-one")
    admin_two = await _seed_admin(wire_db_session, username="admin-two")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-twoadmins")

    resp = client.patch(f"/api/admin/users/{admin_two.id}", json={"role": "user"})
    assert resp.status_code == 200


async def test_cannot_delete_the_last_active_admin(monkeypatch, wire_db_session):
    only_admin = await _seed_admin(wire_db_session, username="lonely-admin-3")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-deletelast")

    resp = client.delete(f"/api/admin/users/{only_admin.id}")
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


async def test_admin_can_delete_a_non_admin_user(monkeypatch, wire_db_session):
    from sqlalchemy import select

    from app.db.models import User as UserRow

    await _seed_admin(wire_db_session, username="root-admin-2")
    target = await _seed_admin(wire_db_session, username="doomed", role="user")
    client = _client_as(monkeypatch, wire_db_session, role="admin", sid="s-delete")

    resp = client.delete(f"/api/admin/users/{target.id}")
    assert resp.status_code == 204

    row = await wire_db_session.scalar(select(UserRow).where(UserRow.username == "doomed"))
    assert row is None
