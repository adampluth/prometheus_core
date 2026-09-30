"""Proofs that session state really is shared, not process-local (Tier 4, stream A).

Every other test in this suite runs against a single Valkey client and so cannot tell
a shared store from the process-local dicts this replaced. These build a *second*
client over the same ``FakeServer`` — the ``valkey_server`` fixture — and model two
``uvicorn --workers N`` processes: one writes, the other must see it. That is the whole
claim ``BACKEND_WORKERS`` rests on.

The complementary guarantees live next door: ``test_events.py`` covers the pub/sub
event bus across workers and ``claim_task``'s exactly-once ``SREM``.
"""

from typing import Any

from fastapi.testclient import TestClient


def _other_worker(valkey_server: Any) -> Any:
    """A second Valkey client over the same data — i.e. a second backend worker."""
    from fakeredis.aioredis import FakeRedis

    return FakeRedis(server=valkey_server, decode_responses=True)


async def test_needs_info_counter_increments_across_workers(session_store, valkey_server):
    """The follow-up depth cap counts turns, wherever each turn lands.

    ``consecutive_needs_info`` was an attribute on an in-process object, so two workers
    each counted their own turns and the cap (T-2) never tripped under load balancing.
    It is an ``HINCRBY`` on one hash now, so the count is the conversation's, not the
    worker's.
    """
    from app.sessions import SessionStore

    worker_b = SessionStore(_other_worker(valkey_server))

    await session_store.reset("sid-mw", "c1")
    assert await session_store.bump_needs_info("sid-mw", conversation_id="c1") == 1
    assert await worker_b.bump_needs_info("sid-mw", conversation_id="c1") == 2
    assert await session_store.bump_needs_info("sid-mw", conversation_id="c1") == 3

    # Both workers read the same total, and a reset on one clears it for the other.
    assert (await worker_b.get_or_create("sid-mw")).consecutive_needs_info == 3
    await worker_b.reset_needs_info("sid-mw", conversation_id="c1")
    assert (await session_store.get_or_create("sid-mw")).consecutive_needs_info == 0


async def test_conversation_binding_is_visible_to_the_other_worker(session_store, valkey_server):
    """A turn handled by worker B continues the conversation worker A created."""
    from app.sessions import SessionStore

    worker_b = SessionStore(_other_worker(valkey_server))

    await session_store.reset("sid-mw", "c1")
    await session_store.set_artifacts("sid-mw", [{"artifact_id": "a1"}], conversation_id="c1")

    snapshot = await worker_b.get("sid-mw")
    assert snapshot is not None
    assert snapshot.conversation_id == "c1"
    assert snapshot.artifacts == [{"artifact_id": "a1"}]


async def test_revoked_sid_401s_on_the_other_worker(monkeypatch, fake_redis, valkey_server):
    """Logging out on one worker logs you out everywhere.

    ``_revoked_sids`` used to be a process-local dict, so a retained cookie kept working
    on every worker that had not served the logout (and on every worker after a restart).
    The marker is a ``revoked:sid:{sid}`` key now, checked by ``current_claims``.
    """
    from app import security
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)

    token = security.issue_token(security.User(identifier="alice"), sid="sid-revoke-mw")
    client = TestClient(app)  # served with `fake_redis` -- "worker B"
    client.cookies.set(settings.cookie_name, token)
    assert client.get("/api/auth/me").status_code == 200

    # Worker A handles the logout.
    claims = security.verify_token(token)
    await security.revoke_session(_other_worker(valkey_server), claims.sid, claims.exp)

    assert client.get("/api/auth/me").status_code == 401


async def test_revocation_check_fails_open(monkeypatch):
    """A Valkey outage must not 401 every logged-in user.

    Deliberate direction: failing closed would lock everyone out whenever Valkey blips,
    while failing open leaves a logged-out session usable until its token expires --
    which is exactly what the old in-process dict did after any restart.
    """
    from app import security

    class _Down:
        async def exists(self, *_args: Any) -> int:
            raise ConnectionError("valkey is down")

    assert await security.is_revoked(_Down(), "sid-any") is False
