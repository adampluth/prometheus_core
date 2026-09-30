"""Offline tests for GET /api/conversations/recent (task 08).

Drive the endpoint against a ``TestClient`` with ``ConversationStateClient``
replaced by an in-memory fake — no live Redis. Covers the Redis-down graceful
degrade, populated results, auth, and limit validation. The 200 cases also prove
the router is mounted in ``app/main.py``.
"""

from fastapi.testclient import TestClient

from app.routers import conversations as conversations_router


class _FakeClient:
    """Stand-in for ConversationStateClient with canned availability + rows."""

    def __init__(self, *, available: bool, rows: list[dict]):
        self._available = available
        self._rows = rows
        self.last_limit: int | None = None
        self.last_user_id: str | None = None

    @property
    def available(self) -> bool:
        return self._available

    def get_conversation_history(self, limit: int = 20, user_id: str | None = None) -> list[dict]:
        self.last_limit = limit
        self.last_user_id = user_id
        return self._rows[:limit]


def _patch_client(monkeypatch, *, available: bool, rows: list[dict]) -> _FakeClient:
    fake = _FakeClient(available=available, rows=rows)
    monkeypatch.setattr(conversations_router, "ConversationStateClient", lambda _url: fake)
    return fake


def _authed_client(monkeypatch) -> TestClient:
    from app import security
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)

    token = security.issue_token(security.User(identifier="alice"), sid="sid-conv")
    client = TestClient(app)
    client.cookies.set(settings.cookie_name, token)
    return client


def test_recent_requires_auth():
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/conversations/recent")
    assert resp.status_code == 401


def test_recent_degrades_gracefully_when_redis_down(monkeypatch):
    _patch_client(monkeypatch, available=False, rows=[])
    client = _authed_client(monkeypatch)

    resp = client.get("/api/conversations/recent")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "conversations": []}


def test_recent_returns_conversations(monkeypatch):
    rows = [
        {
            "conversation_id": "c2",
            "query": "corrosion rate?",
            "status": "completed",
            "material_id": "SS316",
            "timestamp": "2026-08-11T10:00:00Z",
        },
        {
            "conversation_id": "c1",
            "query": "hello",
            "status": "unknown",
            "material_id": None,
            "timestamp": "2026-08-10T09:00:00Z",
        },
    ]
    _patch_client(monkeypatch, available=True, rows=rows)
    client = _authed_client(monkeypatch)

    resp = client.get("/api/conversations/recent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert [c["conversation_id"] for c in body["conversations"]] == ["c2", "c1"]
    assert body["conversations"][0]["material_id"] == "SS316"
    assert body["conversations"][1]["material_id"] is None


def test_recent_scopes_by_authenticated_user(monkeypatch):
    """GET /api/conversations/recent must pass the caller's own user_id, not
    some other user's -- this is the fix for the pre-multi-user global scan."""
    fake = _patch_client(monkeypatch, available=True, rows=[])
    client = _authed_client(monkeypatch)  # issues a token for identifier="alice"

    resp = client.get("/api/conversations/recent")
    assert resp.status_code == 200
    assert fake.last_user_id == "alice"


def test_recent_forwards_limit(monkeypatch):
    fake = _patch_client(monkeypatch, available=True, rows=[])
    client = _authed_client(monkeypatch)

    resp = client.get("/api/conversations/recent?limit=5")
    assert resp.status_code == 200
    assert fake.last_limit == 5


def test_recent_rejects_out_of_range_limit(monkeypatch):
    _patch_client(monkeypatch, available=True, rows=[])
    client = _authed_client(monkeypatch)

    assert client.get("/api/conversations/recent?limit=0").status_code == 422
    assert client.get("/api/conversations/recent?limit=1000").status_code == 422


# --------------------------------------------------------------------------- #
# AU-2 — ConversationStateClient sorts before slicing (returns the newest)
# --------------------------------------------------------------------------- #


def test_get_history_sorts_before_slicing(monkeypatch):
    """With more conversations than the limit, the newest are returned — not an
    arbitrary unordered subset (the old slice-before-sort bug)."""
    import json as _json

    from app.conversation_state import ConversationStateClient

    # keys() returns them oldest-first; a slice-before-sort with limit=2 would keep
    # {old, mid} and drop the newest. Sort-before-slice must return {new, mid}.
    blobs = {
        "conversation:old": {"user_query": "old", "conversation_history": [{"timestamp": "2026-01-01T00:00:00Z"}]},
        "conversation:mid": {"user_query": "mid", "conversation_history": [{"timestamp": "2026-05-01T00:00:00Z"}]},
        "conversation:new": {"user_query": "new", "conversation_history": [{"timestamp": "2026-08-01T00:00:00Z"}]},
    }

    class _FakeRedis:
        def keys(self, pattern):
            return list(blobs.keys())

        def get(self, key):
            return _json.dumps(blobs[key])

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    rows = c.get_conversation_history(limit=2)
    assert [r["conversation_id"] for r in rows] == ["new", "mid"]


# --------------------------------------------------------------------------- #
# Per-user history scoping (user:convs:{user_id} index, not a global scan)
# --------------------------------------------------------------------------- #


def test_get_history_scopes_to_user_id_via_index(monkeypatch):
    """With user_id set, only conversations in that user's index are read --
    conversations belonging to other users must never be scanned or returned."""
    import json as _json

    from app.conversation_state import ConversationStateClient

    blobs = {
        "conversation:mine": {"user_query": "mine", "conversation_history": [{"timestamp": "2026-08-01T00:00:00Z"}]},
        "conversation:theirs": {
            "user_query": "theirs",
            "conversation_history": [{"timestamp": "2026-08-02T00:00:00Z"}],
        },
    }

    class _FakeRedis:
        def keys(self, pattern):  # must NOT be called on the scoped path
            raise AssertionError("global keys() scan used despite user_id being provided")

        def smembers(self, key):
            assert key == "user:convs:alice"
            return {"mine"}

        def get(self, key):
            return _json.dumps(blobs[key])

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    rows = c.get_conversation_history(limit=20, user_id="alice")
    assert [r["conversation_id"] for r in rows] == ["mine"]


def test_get_history_falls_back_to_global_scan_without_user_id(monkeypatch):
    """user_id=None preserves the legacy global scan (pre-ownership-tracking data)."""
    import json as _json

    from app.conversation_state import ConversationStateClient

    blobs = {
        "conversation:a": {"user_query": "a", "conversation_history": [{"timestamp": "2026-08-01T00:00:00Z"}]},
    }

    class _FakeRedis:
        def keys(self, pattern):
            assert pattern == "conversation:*"
            return list(blobs.keys())

        def get(self, key):
            return _json.dumps(blobs[key])

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    rows = c.get_conversation_history(limit=20)
    assert [r["conversation_id"] for r in rows] == ["a"]


# --------------------------------------------------------------------------- #
# Chat history: ownership, soft delete, title precedence (ConversationStateClient)
# --------------------------------------------------------------------------- #


def test_owns_checks_the_user_index(monkeypatch):
    from app.conversation_state import ConversationStateClient

    class _FakeRedis:
        def sismember(self, key, member):
            assert key == "user:convs:alice"
            return member == "c1"

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    assert c.owns("alice", "c1") is True
    assert c.owns("alice", "c2") is False


def test_get_history_excludes_soft_deleted_conversations(monkeypatch):
    import json as _json

    from app.conversation_state import ConversationStateClient

    blobs = {
        "conversation:visible": {"user_query": "keep", "conversation_history": [{"timestamp": "2026-08-01T00:00:00Z"}]},
        "conversation:hidden": {"user_query": "gone", "conversation_history": [{"timestamp": "2026-08-02T00:00:00Z"}]},
    }
    meta = {"conv:meta:hidden": {"hidden": "1", "hidden_at": "2026-08-03T00:00:00Z"}}

    class _FakeRedis:
        def keys(self, pattern):
            return list(blobs.keys())

        def get(self, key):
            return _json.dumps(blobs[key])

        def hgetall(self, key):
            return meta.get(key, {})

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    rows = c.get_conversation_history(limit=20)
    assert [r["conversation_id"] for r in rows] == ["visible"]


def test_get_history_title_precedence(monkeypatch):
    """A user rename wins over the client-agent's LLM title, which wins over the
    raw first query."""
    import json as _json

    from app.conversation_state import ConversationStateClient

    blobs = {
        "conversation:renamed": {
            "user_query": "raw query",
            "title": "llm title",
            "conversation_history": [{"timestamp": "2026-08-01T00:00:00Z"}],
        },
        "conversation:llm-only": {
            "user_query": "raw query 2",
            "title": "llm title 2",
            "conversation_history": [{"timestamp": "2026-08-01T00:00:01Z"}],
        },
        "conversation:no-title": {
            "user_query": "raw query 3",
            "conversation_history": [{"timestamp": "2026-08-01T00:00:02Z"}],
        },
    }
    meta = {"conv:meta:renamed": {"title_override": "my rename"}}

    class _FakeRedis:
        def keys(self, pattern):
            return list(blobs.keys())

        def get(self, key):
            return _json.dumps(blobs[key])

        def hgetall(self, key):
            return meta.get(key, {})

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    rows = {r["conversation_id"]: r for r in c.get_conversation_history(limit=20)}
    assert rows["renamed"]["title"] == "my rename"
    assert rows["llm-only"]["title"] == "llm title 2"
    assert rows["no-title"]["title"] == "raw query 3"


def test_is_pending_reflects_agent_status_final_flag(monkeypatch):
    import json as _json

    from app.conversation_state import ConversationStateClient

    status = {
        "agent:status:running": _json.dumps({"message": "Executing…", "final": False}),
        "agent:status:done": _json.dumps({"message": "Response ready.", "final": True}),
    }

    class _FakeRedis:
        def get(self, key):
            return status.get(key)

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    assert c.is_pending("running") is True
    assert c.is_pending("done") is False
    assert c.is_pending("never-started") is False  # no key at all -> not pending


def test_get_history_marks_actively_running_conversations_pending(monkeypatch):
    import json as _json

    from app.conversation_state import ConversationStateClient

    blobs = {
        "conversation:running": {
            "user_query": "still going",
            "conversation_history": [{"timestamp": "2026-08-01T00:00:00Z"}],
        },
        "conversation:done": {
            "user_query": "finished",
            "conversation_history": [{"timestamp": "2026-08-01T00:00:01Z"}],
        },
    }
    status = {"agent:status:running": _json.dumps({"message": "…", "final": False})}

    class _FakeRedis:
        """Serves both `conversation:{id}` and `agent:status:{id}` reads —
        `is_pending` calls `.get()` on the same client the history scan uses."""

        def keys(self, pattern):
            return list(blobs.keys()) if pattern == "conversation:*" else []

        def get(self, key):
            # `blobs` is keyed by the full "conversation:{id}" key, matching what
            # the history scan passes here -- not by the bare id.
            if key.startswith("conversation:"):
                return _json.dumps(blobs[key]) if key in blobs else None
            return status.get(key)

        def hgetall(self, key):
            return {}

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    rows = {r["conversation_id"]: r for r in c.get_conversation_history(limit=20)}
    assert rows["running"]["pending"] is True
    assert rows["done"]["pending"] is False


def test_get_transcript_normalizes_and_drops_orchestration_noise(monkeypatch):
    import json as _json

    from app.conversation_state import ConversationStateClient

    conv = {
        "conversation_history": [
            {"role": "user", "content": "hello", "timestamp": "t1"},
            {"routing_decision": {"primary_skill": "corrosion"}},  # no role -> dropped
            {"role": "assistant", "content": "hi there", "timestamp": "t2"},
            {"role": "assistant", "content": "", "timestamp": "t3"},  # empty -> dropped
        ],
    }

    class _FakeRedis:
        def get(self, key):
            assert key == "conversation:c1"
            return _json.dumps(conv)

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    messages = c.get_transcript("c1")
    assert messages == [
        {"role": "user", "content": "hello", "timestamp": "t1"},
        {"role": "assistant", "content": "hi there", "timestamp": "t2"},
    ]


def test_get_transcript_collapses_the_duplicate_final_report_turn(monkeypatch):
    """The client-agent logs each answer twice (``agent_response_*`` then
    ``final_report``) with identical content; the transcript must show it once."""
    import json as _json

    from app.conversation_state import ConversationStateClient

    conv = {
        "conversation_history": [
            {"role": "user", "content": "mesh it", "timestamp": "t1"},
            {"role": "assistant", "content": "done", "type": "agent_response_meshing", "timestamp": "t2"},
            {"role": "assistant", "content": "done", "type": "final_report", "timestamp": "t3"},
            {"role": "user", "content": "again", "timestamp": "t4"},
            # Same content as an earlier turn but NOT adjacent -> kept.
            {"role": "assistant", "content": "done", "type": "final_report", "timestamp": "t5"},
        ],
    }

    class _FakeRedis:
        def get(self, key):
            return _json.dumps(conv)

    c = ConversationStateClient("redis://x")
    monkeypatch.setattr(type(c), "client", property(lambda self: _FakeRedis()))

    assert c.get_transcript("c1") == [
        {"role": "user", "content": "mesh it", "timestamp": "t1"},
        {"role": "assistant", "content": "done", "timestamp": "t2"},
        {"role": "user", "content": "again", "timestamp": "t4"},
        {"role": "assistant", "content": "done", "timestamp": "t5"},
    ]


# --------------------------------------------------------------------------- #
# GET /{id}, PATCH /{id}, DELETE /{id}, POST /{id}/resume
# --------------------------------------------------------------------------- #


class _FakeDetailClient:
    """Stand-in for ConversationStateClient backing the per-conversation endpoints."""

    def __init__(self, *, conversations: dict, owners: dict):
        self._conversations = conversations
        self._owners = owners
        self._meta: dict = {}

    @property
    def available(self) -> bool:
        return True

    def owns(self, user_id: str, conv_id: str) -> bool:
        return self._owners.get(conv_id) == user_id

    def get_conversation(self, conv_id: str):
        return self._conversations.get(conv_id)

    def get_meta(self, conv_id: str) -> dict:
        return self._meta.get(conv_id, {})

    def set_title_override(self, conv_id: str, title: str) -> bool:
        self._meta.setdefault(conv_id, {})["title_override"] = title
        return True

    def set_hidden(self, conv_id: str, hidden: bool) -> bool:
        self._meta.setdefault(conv_id, {})["hidden"] = "1" if hidden else None
        return True

    def get_transcript(self, conv_id: str) -> list[dict]:
        conv = self._conversations.get(conv_id) or {}
        history = conv.get("conversation_history", []) or []
        return [
            {"role": e["role"], "content": e["content"], "timestamp": e.get("timestamp", "")}
            for e in history
            if isinstance(e, dict) and e.get("role") in ("user", "assistant") and e.get("content")
        ]

    def get_artifacts(self, conv_id: str) -> list[dict]:
        return []


def _detail_client(monkeypatch, *, conversations: dict, owners: dict) -> _FakeDetailClient:
    fake = _FakeDetailClient(conversations=conversations, owners=owners)
    monkeypatch.setattr(conversations_router, "ConversationStateClient", lambda _url: fake)
    return fake


def test_rename_requires_ownership(monkeypatch):
    _detail_client(monkeypatch, conversations={"c1": {}}, owners={"c1": "bob"})
    client = _authed_client(monkeypatch)  # issues a token for "alice"

    resp = client.patch("/api/conversations/c1", json={"title": "New title"})
    assert resp.status_code == 404


def test_rename_sets_title_override(monkeypatch):
    fake = _detail_client(monkeypatch, conversations={"c1": {}}, owners={"c1": "alice"})
    client = _authed_client(monkeypatch)

    resp = client.patch("/api/conversations/c1", json={"title": "New title"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New title"
    assert fake.get_meta("c1")["title_override"] == "New title"


def test_rename_rejects_empty_title(monkeypatch):
    _detail_client(monkeypatch, conversations={"c1": {}}, owners={"c1": "alice"})
    client = _authed_client(monkeypatch)

    resp = client.patch("/api/conversations/c1", json={"title": "   "})
    assert resp.status_code == 422


def test_delete_requires_ownership(monkeypatch):
    _detail_client(monkeypatch, conversations={"c1": {}}, owners={"c1": "bob"})
    client = _authed_client(monkeypatch)

    resp = client.delete("/api/conversations/c1")
    assert resp.status_code == 404


def test_delete_soft_hides_without_touching_the_conversation_document(monkeypatch):
    fake = _detail_client(monkeypatch, conversations={"c1": {"user_query": "hi"}}, owners={"c1": "alice"})
    client = _authed_client(monkeypatch)

    resp = client.delete("/api/conversations/c1")
    assert resp.status_code == 204
    assert fake.get_meta("c1")["hidden"] == "1"
    assert fake.get_conversation("c1") == {"user_query": "hi"}  # untouched


def test_resume_requires_ownership(monkeypatch):
    _detail_client(monkeypatch, conversations={"c1": {}}, owners={"c1": "bob"})
    client = _authed_client(monkeypatch)

    resp = client.post("/api/conversations/c1/resume")
    assert resp.status_code == 404


async def test_resume_rebinds_session_and_returns_transcript(monkeypatch, session_store):
    conv = {
        "user_query": "hello",
        "conversation_history": [
            {"role": "user", "content": "hello", "timestamp": "t1"},
            {"role": "assistant", "content": "hi there", "timestamp": "t2"},
        ],
    }
    _detail_client(monkeypatch, conversations={"c1": conv}, owners={"c1": "alice"})
    client = _authed_client(monkeypatch)

    resp = client.post("/api/conversations/c1/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == "c1"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

    session = await session_store.get("sid-conv")
    assert session is not None
    assert session.conversation_id == "c1"


def test_reopening_conversation_does_not_build_previews(monkeypatch):
    """Reopening must stay cheap: artifacts needing a preview carry a lazy
    ``preview_url`` instead of being processed up front (the viewer fetches it once
    scrolled into view)."""
    from app.services.previews import pool as preview_pool

    async def _boom(*_):
        raise AssertionError("reopening a conversation must not build previews")

    monkeypatch.setattr(preview_pool, "run_build", _boom)

    mesh = {"artifact_id": "m1", "name": "mesh.exo", "mime_type": "application/x-exodus", "uri": "file:///x/mesh.exo"}
    conv = {"user_query": "mesh it", "conversation_history": [{"role": "assistant", "content": "done"}]}
    fake = _detail_client(monkeypatch, conversations={"c1": conv}, owners={"c1": "alice"})
    monkeypatch.setattr(fake, "get_artifacts", lambda _conv_id: [dict(mesh)])
    client = _authed_client(monkeypatch)

    for resp in (client.get("/api/conversations/c1"), client.post("/api/conversations/c1/resume")):
        assert resp.status_code == 200
        [artifact] = resp.json()["messages"][0]["artifacts"]
        assert artifact["render_hint"] == "mesh"
        assert artifact["json_body"] is None
        assert artifact["preview_url"] == "/api/conversations/c1/artifacts/m1/preview"


def test_detail_attaches_artifacts_to_the_turn_that_produced_them(monkeypatch):
    conv = {
        "conversation_history": [
            {"role": "user", "content": "plot it", "timestamp": "2026-09-01T10:00:00+00:00"},
            {"role": "assistant", "content": "plotted", "timestamp": "2026-09-01T10:01:00+00:00"},
            {"role": "user", "content": "again", "timestamp": "2026-09-01T11:00:00+00:00"},
            {"role": "assistant", "content": "replotted", "timestamp": "2026-09-01T11:01:00+00:00"},
        ],
    }
    artifacts = [
        {"artifact_id": "a", "name": "a.png", "created_at": "2026-09-01T10:00:30+00:00"},
        {"artifact_id": "b", "name": "b.png", "created_at": "2026-09-01T11:00:30+00:00"},
        {"artifact_id": "c", "name": "c.png"},  # no timestamp → last reply
        {"artifact_id": "d", "name": "d.png", "created_at": "2026-09-01T12:00:00+00:00"},  # after every reply
    ]
    fake = _detail_client(monkeypatch, conversations={"c1": conv}, owners={"c1": "alice"})
    monkeypatch.setattr(fake, "get_artifacts", lambda _conv_id: artifacts)
    client = _authed_client(monkeypatch)

    messages = client.get("/api/conversations/c1").json()["messages"]
    assert [[a["artifact_id"] for a in m["artifacts"]] for m in messages] == [[], ["a"], [], ["b", "c", "d"]]


def test_detail_keeps_artifacts_when_transcript_has_no_reply(monkeypatch):
    conv = {"conversation_history": [{"role": "user", "content": "go", "timestamp": "2026-09-01T10:00:00+00:00"}]}
    fake = _detail_client(monkeypatch, conversations={"c1": conv}, owners={"c1": "alice"})
    monkeypatch.setattr(fake, "get_artifacts", lambda _conv_id: [{"artifact_id": "a", "name": "a.png"}])
    client = _authed_client(monkeypatch)

    messages = client.get("/api/conversations/c1").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert [a["artifact_id"] for a in messages[1]["artifacts"]] == ["a"]


async def test_resume_seeds_session_artifacts_with_the_catalog(monkeypatch, session_store):
    """``session.artifacts`` (what the next turn hands the client-agent) is the catalog
    resume already read."""
    fake = _detail_client(monkeypatch, conversations={"c1": {}}, owners={"c1": "alice"})
    monkeypatch.setattr(fake, "get_artifacts", lambda conv_id: [{"artifact_id": "a1", "name": "n"}], raising=False)
    client = _authed_client(monkeypatch)

    assert client.post("/api/conversations/c1/resume").status_code == 200
    session = await session_store.get_or_create("sid-conv")
    assert session.conversation_id == "c1"
    assert [a["artifact_id"] for a in session.artifacts] == ["a1"]


async def test_superseded_resume_does_not_rebind_or_seed_the_session(monkeypatch, session_store):
    """Click A, then B: the session must end on B whichever request finishes last.

    Rebinds are ordered by ``bind_seq`` (``SessionStore.reset``), so A's resume landing
    after B's is still answered — the frontend drops it as superseded — but changes
    nothing.
    """
    fake = _detail_client(monkeypatch, conversations={"A": {}, "B": {}}, owners={"A": "alice", "B": "alice"})
    monkeypatch.setattr(
        fake, "get_artifacts", lambda conv_id: [{"artifact_id": f"{conv_id}-art", "name": "n"}], raising=False
    )
    client = _authed_client(monkeypatch)

    assert client.post("/api/conversations/B/resume", json={"bind_seq": 20}).status_code == 200
    assert client.post("/api/conversations/A/resume", json={"bind_seq": 10}).status_code == 200

    session = await session_store.get_or_create("sid-conv")
    assert session.conversation_id == "B"
    assert [a["artifact_id"] for a in session.artifacts] == ["B-art"]
