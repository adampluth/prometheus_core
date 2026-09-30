"""Offline tests for the uploads router + service (task 06).

Exercise ``POST /api/uploads`` against a ``TestClient`` with
``upload_service.stage_upload`` stubbed (no live agent), plus the pure
``guess_upload_mime`` MIME resolution. The endpoint-mount checks also guard
against the router being left unregistered in ``app/main.py``.
"""

from fastapi.testclient import TestClient

from app.contract import UploadDescriptor
from app.services import uploads as upload_service


def _authed_client(monkeypatch) -> TestClient:
    """Build a TestClient with a valid session cookie."""
    from app import security
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)

    token = security.issue_token(security.User(identifier="alice"), sid="sid-upload")
    client = TestClient(app)
    client.cookies.set(settings.cookie_name, token)
    return client


# --------------------------------------------------------------------------- #
# guess_upload_mime — pure MIME resolution
# --------------------------------------------------------------------------- #


def test_guess_upload_mime_prefers_concrete_client_mime():
    assert upload_service.guess_upload_mime("f.bin", "application/pdf") == "application/pdf"


def test_guess_upload_mime_maps_cad_extensions():
    assert upload_service.guess_upload_mime("part.stp") == "model/step"
    assert upload_service.guess_upload_mime("part.STEP", "application/octet-stream") == "model/step"


def test_guess_upload_mime_falls_back_to_octet_stream():
    assert upload_service.guess_upload_mime("mystery", "") == "application/octet-stream"


# --------------------------------------------------------------------------- #
# POST /api/uploads — endpoint wiring, staging, limits
# --------------------------------------------------------------------------- #


def test_uploads_requires_auth():
    from app.main import app

    client = TestClient(app)
    resp = client.post("/api/uploads", files={"file": ("f.txt", b"hi", "text/plain")})
    assert resp.status_code == 401


def test_upload_small_file_returns_descriptor(monkeypatch):
    client = _authed_client(monkeypatch)

    captured: dict = {}

    async def _stub_stage(
        a2a_client, file_path, *, name, conversation_id, element_mime=None, size_bytes=None, user_token=None
    ):
        captured.update(
            name=name, conversation_id=conversation_id, size_bytes=size_bytes, user_token=user_token
        )
        return UploadDescriptor(
            upload_id="up-1",
            name=name,
            mime_type=upload_service.guess_upload_mime(name, element_mime),
            size_bytes=size_bytes or 0,
        )

    monkeypatch.setattr(upload_service, "stage_upload", _stub_stage)

    resp = client.post("/api/uploads", files={"file": ("part.step", b"solid data", "application/octet-stream")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["upload_id"] == "up-1"
    assert body["mime_type"] == "model/step"  # CAD extension mapping survives
    assert body["size_bytes"] == len(b"solid data")
    # A conversation id is minted so the upload can attach even before the first turn.
    assert captured["conversation_id"]
    # No USER_IDENTITY_PRIVATE_KEY configured in this test env, so mint_for_user
    # degrades to None rather than raising -- the router must still succeed.
    assert captured["user_token"] is None


def test_upload_passes_minted_user_token(monkeypatch):
    """When identity is configured, the router mints and forwards a user_token."""
    client = _authed_client(monkeypatch)

    from app import identity

    async def _mint(user_id: str) -> str:
        return f"token-for-{user_id}"

    monkeypatch.setattr(identity, "mint_for_user", _mint)

    captured: dict = {}

    async def _stub_stage(
        a2a_client, file_path, *, name, conversation_id, element_mime=None, size_bytes=None, user_token=None
    ):
        captured["user_token"] = user_token
        return UploadDescriptor(upload_id="up-2", name=name, mime_type="text/plain", size_bytes=size_bytes or 0)

    monkeypatch.setattr(upload_service, "stage_upload", _stub_stage)

    resp = client.post("/api/uploads", files={"file": ("f.txt", b"hi", "text/plain")})
    assert resp.status_code == 200
    assert captured["user_token"] == "token-for-alice"


def test_upload_over_limit_returns_413(monkeypatch):
    client = _authed_client(monkeypatch)
    monkeypatch.setattr(upload_service, "MAX_UPLOAD_BYTES", 4)

    async def _should_not_run(*args, **kwargs):  # pragma: no cover - must never stage
        raise AssertionError("stage_upload should not be called for an over-limit file")

    monkeypatch.setattr(upload_service, "stage_upload", _should_not_run)

    resp = client.post("/api/uploads", files={"file": ("big.bin", b"way too many bytes", "application/octet-stream")})
    assert resp.status_code == 413


def test_upload_staging_failure_returns_502(monkeypatch):
    client = _authed_client(monkeypatch)

    async def _boom(*args, **kwargs):
        raise upload_service.UploadStagingError("staging refused")

    monkeypatch.setattr(upload_service, "stage_upload", _boom)

    resp = client.post("/api/uploads", files={"file": ("f.txt", b"hi", "text/plain")})
    assert resp.status_code == 502
