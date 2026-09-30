"""GET /.well-known/jwks.json -- public, unauthenticated, DB-backed."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_jwks_is_unauthenticated_and_empty_with_no_keys(wire_db_session):
    from app.main import app

    resp = TestClient(app).get("/.well-known/jwks.json")
    assert resp.status_code == 200
    assert resp.json() == {"keys": []}


async def test_jwks_serves_generated_keys(wire_db_session):
    from app import signing_keys
    from app.crypto import encrypt
    from app.db.models import SigningKey
    from app.main import app

    kid, private_pem, public_pem = signing_keys.generate_signing_key()
    wire_db_session.add(
        SigningKey(kid=kid, private_key_encrypted=encrypt(private_pem), public_key_pem=public_pem, is_active=True)
    )
    await wire_db_session.commit()

    resp = TestClient(app).get("/.well-known/jwks.json")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["kid"] == kid
    assert keys[0]["kty"] == "RSA"
    assert keys[0]["use"] == "sig"
