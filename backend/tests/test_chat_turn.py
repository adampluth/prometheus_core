"""Offline tests for the turn orchestrator (task 04).

These exercise the pure ``ConversationResponse`` → ``ChatMessageResponse``
mapping and the ``handle_turn`` glue with a stubbed ``A2AClient`` — no live agent
or Redis. The router's error-to-inline-response behavior is covered against a
``TestClient`` with a monkeypatched service.
"""

import pytest
from fastapi.testclient import TestClient

from app.a2a_client import ConversationResponse
from app.contract import ChatMessageResponse, ChatStatus
from app.services import chat as chat_service
from app.sessions import SessionState


def _resp(**kwargs) -> ConversationResponse:
    return ConversationResponse.from_payload({"conversation_id": "c1", **kwargs})


def _session() -> SessionState:
    return SessionState(sid="sid-1", conversation_id="c1")


# --------------------------------------------------------------------------- #
# map_response — per-status branches
# --------------------------------------------------------------------------- #


def test_map_completed_shapes_artifacts_and_persists_catalog():
    session = _session()
    response = _resp(
        status="completed",
        report="The analysis is done.",
        artifacts=[{"artifact_id": "a1", "name": "full"}],
        new_artifacts=[
            {"artifact_id": "a1", "name": "result.json", "mime_type": "application/json"},
            {"artifact_id": "up", "name": "upload", "metadata": {"source": "user-upload"}},
        ],
        message_metadata={"follow_up_suggestions": ["Do X", "Do Y"]},
    )

    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)

    assert mapped.status == ChatStatus.COMPLETED.value
    assert mapped.turn_id == "t1"
    assert mapped.report == "The analysis is done."
    # user-upload echo is dropped; only the real artifact survives.
    assert mapped.artifacts is not None and len(mapped.artifacts) == 1
    assert mapped.artifacts[0].artifact_id == "a1"
    assert mapped.follow_up_suggestions == ["Do X", "Do Y"]
    # Full catalog persisted onto the session for task-07 content fetches.
    assert session.artifacts == [{"artifact_id": "a1", "name": "full"}]


def test_map_completed_slash_command_suppresses_artifacts():
    session = _session()
    response = _resp(
        status="completed",
        report="done",
        new_artifacts=[{"artifact_id": "a1", "name": "x"}],
        message_metadata={"slash_command": True},
    )
    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)
    assert mapped.artifacts is None


def test_map_completed_falls_back_to_artifact_count_text():
    session = _session()
    response = _resp(
        status="completed",
        new_artifacts=[{"artifact_id": "a1", "name": "x", "mime_type": "application/json"}],
    )
    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)
    assert mapped.report == "1 artifact(s) ready."


def test_map_needs_info_normalizes_questions():
    session = _session()
    response = _resp(
        status="needs_info",
        message="Need more",
        followup_questions=[
            {
                "field": "temp",
                "question": "Temperature?",
                "type": "number",
                "required": True,
                "description": "in Celsius",
            },
            {"question": "Material?", "type": "select", "options": ["steel", "copper"]},
        ],
    )
    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)

    assert mapped.status == ChatStatus.NEEDS_INFO.value
    assert mapped.followup_questions is not None and len(mapped.followup_questions) == 2
    first = mapped.followup_questions[0]
    assert first.id == "temp"
    assert first.label == "Temperature?"
    assert first.input_type == "number"
    assert first.help == "in Celsius"
    second = mapped.followup_questions[1]
    assert second.id == "Material?"  # falls back to the question text
    assert second.options is not None
    assert [o.value for o in second.options] == ["steel", "copper"]


def test_map_plan_ready_carries_auto_approve():
    session = _session()
    response = _resp(status="plan_ready", message="Proposed plan", plan={"steps": ["a", "b"]})

    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=True)
    assert mapped.status == ChatStatus.PLAN_READY.value
    assert mapped.plan == {"steps": ["a", "b"]}
    assert mapped.auto_approve is True

    mapped_off = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)
    assert mapped_off.auto_approve is False


def test_map_backgrounded_registers_awaited_task():
    session = _session()
    response = _resp(status="backgrounded", message_metadata={"task_id": "task-42"})

    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)
    assert mapped.status == ChatStatus.BACKGROUNDED.value
    assert mapped.task_id == "task-42"
    assert "task-42" in session.awaited_task_ids


# --------------------------------------------------------------------------- #
# stale — a turn whose session has since moved to a different conversation
# (chat-history "New chat"/resume while a turn is in flight) must not mutate
# session state for the conversation the session now points at.
# --------------------------------------------------------------------------- #


def test_map_final_skips_session_mutation_when_stale():
    session = _session()
    response = _resp(
        status="completed",
        report="done",
        artifacts=[{"artifact_id": "a1", "name": "full"}],
    )

    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False, stale=True)

    # A response is still produced (the original HTTP request needs one)...
    assert mapped.status == ChatStatus.COMPLETED.value
    # ...but the session's artifact catalog — which belongs to whatever
    # conversation the session is *currently* bound to — is untouched.
    assert session.artifacts == []


def test_map_backgrounded_skips_registration_when_stale():
    session = _session()
    response = _resp(status="backgrounded", message_metadata={"task_id": "task-42"})

    chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False, stale=True)

    assert session.awaited_task_ids == set()


def test_map_needs_info_depth_cap_not_applied_when_stale():
    session = _session()
    session.consecutive_needs_info = 5
    ni = _resp(status="needs_info", message="m", followup_questions=[])

    mapped = chat_service.map_response(ni, "t", session, conversation_id="c1", auto_approve=False, stale=True)

    # Still a needs_info response, not the "too many followups" fallback...
    assert mapped.status == ChatStatus.NEEDS_INFO.value
    # ...and the counter for the conversation the session now points at is untouched.
    assert session.consecutive_needs_info == 5


# --------------------------------------------------------------------------- #
# T-8 — one authoritative text field per terminal status (no double-render)
# --------------------------------------------------------------------------- #


def test_answer_text_only_in_response_text():
    session = _session()
    mapped = chat_service.map_response(
        _resp(status="answer", response_text="42", message="noise"), "t", session, conversation_id="c1", auto_approve=False
    )
    assert mapped.response_text == "42"
    assert mapped.report is None
    assert mapped.message is None


def test_completed_text_only_in_report():
    session = _session()
    mapped = chat_service.map_response(
        _resp(status="completed", report="done", response_text="dup", message="noise"),
        "t",
        session,
        conversation_id="c1", auto_approve=False,
    )
    assert mapped.report == "done"
    assert mapped.response_text is None
    assert mapped.message is None


# --------------------------------------------------------------------------- #
# T-2 — follow-up depth cap
# --------------------------------------------------------------------------- #


def test_needs_info_depth_cap_stops_looping():
    session = _session()
    ni = _resp(status="needs_info", followup_questions=[{"field": "f", "question": "Q?"}])
    # The cap allows N consecutive needs_info turns…
    for _ in range(chat_service._MAX_CONSECUTIVE_NEEDS_INFO):
        assert chat_service.map_response(ni, "t", session, conversation_id="c1", auto_approve=False).status == ChatStatus.NEEDS_INFO.value
    # …then stops looping with a plain message instead of another form.
    capped = chat_service.map_response(ni, "t", session, conversation_id="c1", auto_approve=False)
    assert capped.status == ChatStatus.ANSWER.value
    assert capped.followup_questions is None
    assert "restate" in (capped.response_text or "").lower()


def test_needs_info_counter_resets_on_other_status():
    session = _session()
    ni = _resp(status="needs_info", followup_questions=[{"field": "f", "question": "Q?"}])
    chat_service.map_response(ni, "t", session, conversation_id="c1", auto_approve=False)
    chat_service.map_response(ni, "t", session, conversation_id="c1", auto_approve=False)
    assert session.consecutive_needs_info == 2
    chat_service.map_response(_resp(status="answer", response_text="done"), "t", session, conversation_id="c1", auto_approve=False)
    assert session.consecutive_needs_info == 0


def test_map_backgrounded_without_task_id_registers_nothing():
    session = _session()
    response = _resp(status="backgrounded")
    mapped = chat_service.map_response(response, "t1", session, conversation_id="c1", auto_approve=False)
    assert mapped.task_id is None
    assert session.awaited_task_ids == set()


def test_map_executing_and_error_and_unknown():
    session = _session()

    executing = chat_service.map_response(_resp(status="executing"), "t", session, conversation_id="c1", auto_approve=False)
    assert executing.status == ChatStatus.EXECUTING.value
    assert "executing" in (executing.message or "").lower()

    err = chat_service.map_response(_resp(status="error", message="boom"), "t", session, conversation_id="c1", auto_approve=False)
    assert err.status == ChatStatus.ERROR.value
    assert err.message == "boom"

    answer = chat_service.map_response(
        _resp(status="answer", response_text="hi"), "t", session, conversation_id="c1", auto_approve=False
    )
    assert answer.status == ChatStatus.ANSWER.value
    assert answer.response_text == "hi"


# --------------------------------------------------------------------------- #
# handle_turn — glue over a stubbed A2AClient
# --------------------------------------------------------------------------- #


class _StubClient:
    def __init__(self, response: ConversationResponse):
        self._response = response
        self.sent: dict = {}

    async def send(self, **kwargs):
        self.sent = kwargs
        return self._response


async def test_handle_turn_forwards_args_and_maps():
    session = _session()
    session.artifacts = [{"artifact_id": "a1"}]
    client = _StubClient(_resp(status="answer", response_text="ok"))

    mapped = await chat_service.handle_turn(
        client,  # type: ignore[arg-type]
        session,
        turn_id="t1",
        query="hello",
        plan_approved=True,
    )

    assert mapped.status == ChatStatus.ANSWER.value
    assert client.sent["query"] == "hello"
    assert client.sent["conversation_id"] == "c1"
    assert client.sent["plan_approved"] is True
    assert client.sent["artifacts"] == [{"artifact_id": "a1"}]


async def test_handle_turn_forwards_user_token():
    session = _session()
    client = _StubClient(_resp(status="answer", response_text="ok"))

    await chat_service.handle_turn(
        client,  # type: ignore[arg-type]
        session,
        turn_id="t1",
        query="hello",
        user_token="fake.jwt.token",
    )

    assert client.sent["user_token"] == "fake.jwt.token"


async def test_handle_turn_user_token_defaults_to_none():
    session = _session()
    client = _StubClient(_resp(status="answer", response_text="ok"))

    await chat_service.handle_turn(
        client,  # type: ignore[arg-type]
        session,
        turn_id="t1",
        query="hello",
    )

    assert client.sent["user_token"] is None


async def test_handle_turn_requires_conversation_id():
    session = SessionState(sid="sid-x")  # no conversation_id
    client = _StubClient(_resp(status="answer"))
    with pytest.raises(ValueError):
        await chat_service.handle_turn(client, session, turn_id="t", query="q")  # type: ignore[arg-type]


class _ConversationSwitchingStubClient:
    """Simulates a chat-history "New chat"/resume rebinding the session's
    ``conversation_id`` while a turn is still in flight — the exact race
    ``handle_turn``'s ``stale`` computation guards against."""

    def __init__(self, response: ConversationResponse, session: SessionState, new_conversation_id: str):
        self._response = response
        self._session = session
        self._new_conversation_id = new_conversation_id
        self.sent: dict = {}

    async def send(self, **kwargs):
        self.sent = kwargs
        # The outgoing request must have already captured the ORIGINAL conversation_id
        # before this "rebind" happens (asserted by the caller against .sent).
        self._session.conversation_id = self._new_conversation_id
        return self._response


async def test_handle_turn_sends_original_conversation_but_skips_stale_mutation():
    session = _session()  # conversation_id == "c1"
    response = _resp(
        status="completed",
        report="done for c1",
        artifacts=[{"artifact_id": "a1", "name": "c1-catalog"}],
    )
    client = _ConversationSwitchingStubClient(response, session, new_conversation_id="c2")

    mapped = await chat_service.handle_turn(
        client,  # type: ignore[arg-type]
        session,
        turn_id="t1",
        query="hello",
    )

    # The A2A request still targeted the conversation this turn was actually for...
    assert client.sent["conversation_id"] == "c1"
    # ...the caller still gets a normal response back...
    assert mapped.status == ChatStatus.COMPLETED.value
    # ...but the session (now bound to "c2") did not get c1's artifact catalog stomped
    # onto it.
    assert session.conversation_id == "c2"
    assert session.artifacts == []


def test_upload_descriptors_to_dicts_roundtrip():
    from app.contract import UploadDescriptor

    dicts = chat_service.upload_descriptors_to_dicts(
        [UploadDescriptor(upload_id="u1", name="f.stp", mime_type="model/step", size_bytes=10)]
    )
    assert dicts == [{"upload_id": "u1", "name": "f.stp", "mime_type": "model/step", "size_bytes": 10}]
    assert chat_service.upload_descriptors_to_dicts(None) is None


# --------------------------------------------------------------------------- #
# router — A2AClient failures become an inline error response, not a 500
# --------------------------------------------------------------------------- #


def _authed_client(monkeypatch) -> tuple[TestClient, str]:
    """Build a TestClient with a valid session cookie."""
    from app import security
    from app.config import settings
    from app.main import app
    from app.sessions import SessionStore

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)

    app.state.session_store = SessionStore()
    token = security.issue_token(security.User(identifier="alice"), sid="sid-router")
    client = TestClient(app)
    client.cookies.set(settings.cookie_name, token)
    return client, "sid-router"


def test_post_message_maps_send_failure_to_error(monkeypatch):
    client, _sid = _authed_client(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(chat_service, "handle_turn", boom)

    resp = client.post("/api/chat/messages", json={"query": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "Error communicating with agent" in body["message"]
    assert body["turn_id"]


def test_post_message_rejects_empty_request(monkeypatch):
    client, _sid = _authed_client(monkeypatch)
    resp = client.post("/api/chat/messages", json={"query": "   "})
    assert resp.status_code == 422


def test_post_message_uploads_only_uses_default_query(monkeypatch):
    """T-1: a file with no caption is a supported turn, not a 422; the router supplies
    the neutral default query the agent expects."""
    client, _sid = _authed_client(monkeypatch)
    captured: dict = {}

    async def capture(client_arg, session, *, turn_id, query, **kwargs):
        captured["query"] = query
        return ChatMessageResponse(turn_id=turn_id, status=ChatStatus.ANSWER, response_text="ok")

    monkeypatch.setattr(chat_service, "handle_turn", capture)

    resp = client.post(
        "/api/chat/messages",
        json={
            "query": "",
            "upload_ids": [{"upload_id": "u1", "name": "part.stl", "mime_type": "model/stl", "size_bytes": 10}],
        },
    )
    assert resp.status_code == 200
    assert captured["query"] == "Please process the uploaded file(s)."


def test_post_message_requires_auth(monkeypatch):
    from app.main import app
    from app.sessions import SessionStore

    app.state.session_store = SessionStore()
    client = TestClient(app)
    resp = client.post("/api/chat/messages", json={"query": "hi"})
    assert resp.status_code == 401
