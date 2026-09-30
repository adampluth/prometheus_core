"""POST /api/sessions tests — profile validation (remediation step 05, AU-8)."""

from fastapi.testclient import TestClient


def _authed(monkeypatch, *, sid: str = "sid-session") -> TestClient:
    from app import security
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)

    token = security.issue_token(security.User(identifier="alice"), sid=sid)
    client = TestClient(app)
    client.cookies.set(settings.cookie_name, token)
    return client


def test_create_session_defaults_to_beginner(monkeypatch):
    client = _authed(monkeypatch)
    resp = client.post("/api/sessions", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"] == "Beginner Chat"
    assert body["conversation_id"]
    assert body["workflow_suggestions"]  # beginner carries suggestions


def test_create_session_accepts_valid_profile(monkeypatch):
    client = _authed(monkeypatch)
    resp = client.post("/api/sessions", json={"profile": "Advanced Chat"})
    assert resp.status_code == 200
    assert resp.json()["profile"] == "Advanced Chat"
    assert resp.json()["workflow_suggestions"] == []  # advanced: no suggestions


def test_create_session_rejects_unknown_profile(monkeypatch):
    client = _authed(monkeypatch)
    resp = client.post("/api/sessions", json={"profile": "Bogus Profile"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# SessionStore atomicity — writes that used to be read-then-write across an await
# --------------------------------------------------------------------------- #


async def test_ensure_conversation_mints_one_id_under_concurrency(session_store):
    """Parallel first uploads/turns on a fresh sid must agree on one conversation id."""
    import asyncio

    ids = await asyncio.gather(*(session_store.ensure_conversation("sid-new", f"cand-{i}") for i in range(8)))

    assert len(set(ids)) == 1
    assert (await session_store.get_or_create("sid-new")).conversation_id == ids[0]


async def test_ensure_conversation_keeps_an_existing_binding(session_store):
    await session_store.reset("sid-bound", "c1")
    assert await session_store.ensure_conversation("sid-bound", "c-other") == "c1"


async def test_guarded_write_is_skipped_after_a_rebind_on_another_worker(session_store, valkey_server):
    from fakeredis.aioredis import FakeRedis

    from app.sessions import SessionStore

    worker_b = SessionStore(FakeRedis(server=valkey_server, decode_responses=True))
    await session_store.reset("sid-g", "c1")
    await worker_b.reset("sid-g", "c2", artifacts=[{"artifact_id": "c2-art"}])

    assert await session_store.set_artifacts("sid-g", [{"artifact_id": "c1-art"}], conversation_id="c1") is False
    assert await session_store.await_task("sid-g", "t-c1", conversation_id="c1") is False
    assert await session_store.bump_needs_info("sid-g", conversation_id="c1") is None

    snapshot = await session_store.get_or_create("sid-g")
    assert snapshot.artifacts == [{"artifact_id": "c2-art"}]
    assert not snapshot.awaited_task_ids
    assert snapshot.consecutive_needs_info == 0


async def test_reset_with_a_lower_bind_seq_is_ignored(session_store):
    """A slow, superseded resume must not rebind the session after a newer one landed."""
    assert await session_store.reset("sid-seq", "B", bind_seq=20) is True
    assert await session_store.reset("sid-seq", "A", bind_seq=10) is False
    assert (await session_store.get_or_create("sid-seq")).conversation_id == "B"

    # Equal is not newer either; strictly greater wins.
    assert await session_store.reset("sid-seq", "A", bind_seq=20) is False
    assert await session_store.reset("sid-seq", "A", bind_seq=21) is True
    assert (await session_store.get_or_create("sid-seq")).conversation_id == "A"


async def test_reset_to_the_same_conversation_keeps_awaited_tasks(session_store):
    await session_store.reset("sid-same", "c1")
    await session_store.await_task("sid-same", "t1", conversation_id="c1")

    await session_store.reset("sid-same", "c1")
    assert await session_store.is_awaiting("sid-same", "t1")

    await session_store.reset("sid-same", "c2")
    assert not await session_store.is_awaiting("sid-same", "t1")


def test_create_session_honours_bind_seq_ordering(monkeypatch, session_store):
    client = _authed(monkeypatch, sid="sid-bind")
    first = client.post("/api/sessions", json={"bind_seq": 200})
    assert first.status_code == 200
    stale = client.post("/api/sessions", json={"bind_seq": 100})
    assert stale.status_code == 200

    import asyncio

    stored = asyncio.run(session_store.get_or_create("sid-bind"))
    assert stored.conversation_id == first.json()["conversation_id"]
