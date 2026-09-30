"""Offline tests for the SSE events service (task 05).

Exercise the Valkey-backed :class:`EventBus`, the ``run_task_notifier`` port
(root-vs-child filtering, idempotent completion, fast-finish guard, progress
dedup), and the ``GET /api/events`` endpoint's frame shaping — all against
``fakeredis`` with ``task_events`` monkeypatched, so no live Valkey is needed.

Two things changed shape when this moved off in-process state:

- **Publishing is no longer synchronous fan-out.** Every event round-trips through
  Valkey pub/sub and is delivered by a per-sid relay task, so "the queue is empty"
  is only meaningful after the loop has had a chance to run — hence ``_wait_until``
  for positive assertions and a short settle before negative ones.
- **Awaited task ids live in the store, not on a mutable ``SessionState``.** Tests
  seed them with ``await_task`` and assert on ``is_awaiting``.
"""

import asyncio

import pytest

from app import task_events
from app.contract import StatusEvent, TaskDoneEvent, TaskProgressEvent
from app.services import events as events_service
from app.services.events import EventBus, run_task_subscriber

SID = "sid-1"


async def _seed(store, sid: str = SID, *, conversation_id: str = "c1", awaited=()) -> None:
    """Put a session in the store with ``awaited`` background tasks registered."""
    await store.reset(sid, conversation_id)
    for task_id in awaited:
        await store.await_task(sid, task_id, conversation_id=conversation_id)


async def _drain(queue: "asyncio.Queue", n: int) -> list:
    return [await asyncio.wait_for(queue.get(), timeout=2.0) for _ in range(n)]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true — pub/sub delivery is asynchronous now."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met within the timeout")


async def _settle() -> None:
    """Give the relay tasks room to deliver anything in flight.

    Used before asserting that *nothing* arrived; without it such an assertion would
    pass trivially by racing the relay rather than by the event being filtered out.
    """
    await asyncio.sleep(0.1)


# --------------------------------------------------------------------------- #
# EventBus
# --------------------------------------------------------------------------- #


async def test_bus_fans_out_to_all_queues_for_sid(event_bus):
    q1 = await event_bus.register(SID)
    q2 = await event_bus.register(SID)
    other = await event_bus.register("sid-2")

    await event_bus.publish(SID, StatusEvent(turn_id="t1", message="hi"))

    assert (await asyncio.wait_for(q1.get(), 2.0)).message == "hi"
    assert (await asyncio.wait_for(q2.get(), 2.0)).message == "hi"
    await _settle()
    assert other.empty()


async def test_bus_publish_with_no_subscribers_is_noop(event_bus):
    # Must not raise even though nobody is subscribed to the channel.
    await event_bus.publish("nobody", StatusEvent(turn_id="t", message="x"))


async def test_bus_unregister_drops_sid_when_last_queue_closes(event_bus):
    q = await event_bus.register(SID)
    await event_bus.unregister(SID, q)
    assert SID not in event_bus._subscribers
    assert SID not in event_bus._relays
    assert event_bus.local_listener_count(SID) == 0


async def test_bus_has_listeners_is_cluster_wide(fake_redis, valkey_server):
    """``has_listeners`` asks Valkey, so it sees a stream held by another worker.

    Two ``EventBus`` instances over one server stand in for two uvicorn workers; only
    the second has a local queue, yet both answer ``True``.
    """
    from fakeredis.aioredis import FakeRedis

    worker_a = EventBus(fake_redis)
    worker_b = EventBus(FakeRedis(server=valkey_server, decode_responses=True))

    assert await worker_a.has_listeners(SID) is False
    queue = await worker_b.register(SID)
    assert await worker_a.has_listeners(SID) is True

    # …and a publish from A reaches B's queue, which is the point of the pub/sub bus.
    await worker_a.publish(SID, StatusEvent(turn_id="t", message="cross-worker"))
    assert (await asyncio.wait_for(queue.get(), 2.0)).message == "cross-worker"
    await worker_b.unregister(SID, queue)


async def test_bus_delivers_one_copy_per_queue_with_two_relays(fake_redis, valkey_server):
    """Two workers, one tab each: each queue gets the event exactly once.

    The failure this guards is ``publish`` also delivering locally — the local
    subscriber would then see its own worker's copy *and* the relayed one.
    """
    from fakeredis.aioredis import FakeRedis

    bus_a = EventBus(fake_redis)
    bus_b = EventBus(FakeRedis(server=valkey_server, decode_responses=True))
    qa = await bus_a.register(SID)
    qb = await bus_b.register(SID)

    await bus_a.publish(SID, StatusEvent(turn_id="t", message="once"))

    assert (await asyncio.wait_for(qa.get(), 2.0)).message == "once"
    assert (await asyncio.wait_for(qb.get(), 2.0)).message == "once"
    await _settle()
    assert qa.empty() and qb.empty()

    await bus_a.unregister(SID, qa)
    await bus_b.unregister(SID, qb)


async def test_concurrent_first_registers_share_one_relay(event_bus):
    """Two tabs connecting at once must not both subscribe and orphan a relay.

    Without the register lock each saw no relay, both awaited ``SUBSCRIBE``, and the
    second overwrote the first — whose subscription kept running and delivered every
    event a second time.
    """
    q1, q2 = await asyncio.gather(event_bus.register(SID), event_bus.register(SID))

    assert len(event_bus._relays) == 1
    await event_bus.publish(SID, StatusEvent(turn_id="t", message="once"))
    assert (await asyncio.wait_for(q1.get(), 2.0)).message == "once"
    assert (await asyncio.wait_for(q2.get(), 2.0)).message == "once"
    await _settle()
    assert q1.empty() and q2.empty()

    await event_bus.unregister(SID, q1)
    await event_bus.unregister(SID, q2)
    assert SID not in event_bus._relays


async def test_dead_relay_closes_its_streams_and_forgets_the_sid(event_bus):
    """A relay whose ``listen()`` dies must end its streams, not leave them silent."""
    q1 = await event_bus.register(SID)
    q2 = await event_bus.register(SID)
    relay = event_bus._relays[SID]

    async def broken_listen():
        raise ConnectionError("valkey went away")
        yield  # pragma: no cover - makes this an async generator

    relay.task.cancel()
    await asyncio.gather(relay.task, return_exceptions=True)
    relay.pubsub.listen = broken_listen
    await event_bus._drain(SID, relay)

    assert await asyncio.wait_for(q1.get(), 2.0) is None
    assert await asyncio.wait_for(q2.get(), 2.0) is None
    assert SID not in event_bus._relays
    assert SID not in event_bus._subscribers

    # A reconnecting tab subscribes afresh and receives events again.
    q3 = await event_bus.register(SID)
    await event_bus.publish(SID, StatusEvent(turn_id="t", message="back"))
    assert (await asyncio.wait_for(q3.get(), 2.0)).message == "back"
    await event_bus.unregister(SID, q3)


async def test_progress_is_delivered_locally_not_published(monkeypatch, fake_redis, valkey_server, session_store):
    """Each worker with a tab runs its own task subscriber, so progress must stay local.

    Publishing it handed every tab one copy of each line per worker.
    """
    from fakeredis.aioredis import FakeRedis

    bus_a = EventBus(fake_redis)
    bus_b = EventBus(FakeRedis(server=valkey_server, decode_responses=True))
    qa = await bus_a.register(SID)
    qb = await bus_b.register(SID)
    await _seed(session_store, awaited=["root-1"])
    _patch_task_events(
        monkeypatch,
        events_seq=[{"task_id": "root-1", "root_task_id": "root-1", "state": "running", "message": "step 1"}],
    )

    # Both workers' subscribers see the same task event, as in production.
    await run_task_subscriber(bus_a, session_store, SID, fake_redis)
    await run_task_subscriber(bus_b, session_store, SID, fake_redis)

    assert [e.message for e in await _drain(qa, 1)] == ["step 1"]
    assert [e.message for e in await _drain(qb, 1)] == ["step 1"]
    await _settle()
    assert qa.empty() and qb.empty()

    await bus_a.unregister(SID, qa)
    await bus_b.unregister(SID, qb)


# --------------------------------------------------------------------------- #
# run_task_subscriber — completion, filtering, dedup
# --------------------------------------------------------------------------- #


def _patch_task_events(
    monkeypatch,
    *,
    events_seq=(),
    result=None,
    snapshot=None,
):
    """Stub the Valkey-backed task_events surface used by the subscriber.

    Every function now takes the shared client as its first argument instead of a URL;
    the stubs ignore it.
    """

    async def _subscribe(redis, conversation_id):
        for event in events_seq:
            yield event

    async def _fetch_result(redis, conversation_id, task_id):
        return result

    async def _node_snapshot(redis, conversation_id, task_id):
        return snapshot

    monkeypatch.setattr(task_events, "subscribe", _subscribe)
    monkeypatch.setattr(task_events, "fetch_result", _fetch_result)
    monkeypatch.setattr(task_events, "node_snapshot", _node_snapshot)


async def test_completed_event_emits_mapped_task_done(monkeypatch, fake_redis, event_bus, session_store):
    queue = await event_bus.register(SID)
    await _seed(session_store, awaited=["task-1"])

    _patch_task_events(
        monkeypatch,
        events_seq=[{"task_id": "task-1", "root_task_id": "task-1", "state": "completed"}],
        result={"conversation_id": "c1", "status": "answer", "response_text": "the answer"},
    )

    await run_task_subscriber(event_bus, session_store, SID, fake_redis)

    (event,) = await _drain(queue, 1)
    assert isinstance(event, TaskDoneEvent)
    assert event.task_id == "task-1"
    assert event.state == "completed"
    assert event.response is not None
    assert event.response.response_text == "the answer"
    # Idempotency: the id is consumed once announced.
    assert not await session_store.is_awaiting(SID, "task-1")


async def test_non_completed_terminal_emits_bodyless_task_done(
    monkeypatch, fake_redis, event_bus, session_store
):
    queue = await event_bus.register(SID)
    await _seed(session_store, awaited=["task-1"])

    _patch_task_events(
        monkeypatch,
        events_seq=[{"task_id": "task-1", "root_task_id": "task-1", "state": "failed"}],
    )

    await run_task_subscriber(event_bus, session_store, SID, fake_redis)

    (event,) = await _drain(queue, 1)
    assert isinstance(event, TaskDoneEvent)
    assert event.state == "failed"
    assert event.response is None
    # E-5: a human-readable line accompanies the non-completed terminal event.
    assert event.message is not None and "failed" in event.message


async def test_child_and_unawaited_events_are_ignored(
    monkeypatch, fake_redis, event_bus, session_store
):
    queue = await event_bus.register(SID)
    await _seed(session_store, awaited=["root-1"])

    _patch_task_events(
        monkeypatch,
        events_seq=[
            # child of root-1 → task_id != root_task_id → ignored
            {"task_id": "child-1", "root_task_id": "root-1", "state": "running", "message": "child work"},
            # a root we're not awaiting → ignored
            {"task_id": "root-2", "root_task_id": "root-2", "state": "completed"},
            # the real root progress → surfaced
            {"task_id": "root-1", "root_task_id": "root-1", "state": "running", "message": "step 1"},
        ],
        result={"conversation_id": "c1", "status": "answer"},
    )

    await run_task_subscriber(event_bus, session_store, SID, fake_redis)

    (event,) = await _drain(queue, 1)
    assert isinstance(event, TaskProgressEvent)
    assert event.task_id == "root-1"
    assert event.message == "step 1"
    await _settle()
    assert queue.empty()
    # root-1 never reached a terminal state, so it stays awaited.
    assert await session_store.is_awaiting(SID, "root-1")


async def test_progress_dedups_consecutive_repeats(monkeypatch, fake_redis, event_bus, session_store):
    queue = await event_bus.register(SID)
    await _seed(session_store, awaited=["root-1"])

    _patch_task_events(
        monkeypatch,
        events_seq=[
            {"task_id": "root-1", "root_task_id": "root-1", "state": "running", "message": "same"},
            {"task_id": "root-1", "root_task_id": "root-1", "state": "running", "message": "same"},
            {"task_id": "root-1", "root_task_id": "root-1", "state": "running", "message": ""},
            {"task_id": "root-1", "root_task_id": "root-1", "state": "running", "message": "next"},
        ],
    )

    await run_task_subscriber(event_bus, session_store, SID, fake_redis)

    events = await _drain(queue, 2)
    assert [e.message for e in events] == ["same", "next"]
    await _settle()
    assert queue.empty()


# --------------------------------------------------------------------------- #
# Fast-finish guard
# --------------------------------------------------------------------------- #


async def test_fast_finish_guard_announces_once(monkeypatch, fake_redis, event_bus, session_store):
    """A task terminal before subscribe is announced by the guard, not twice."""
    queue = await event_bus.register(SID)
    await _seed(session_store, awaited=["task-1"])

    # Snapshot already terminal; the live event stream ALSO carries the terminal
    # event. The guard should consume the id first so the loop can't re-announce.
    _patch_task_events(
        monkeypatch,
        events_seq=[{"task_id": "task-1", "root_task_id": "task-1", "state": "completed"}],
        result={"conversation_id": "c1", "status": "answer", "response_text": "done"},
        snapshot={"state": "completed"},
    )

    await run_task_subscriber(event_bus, session_store, SID, fake_redis)

    events = await _drain(queue, 1)
    assert len(events) == 1
    assert isinstance(events[0], TaskDoneEvent)
    assert events[0].response.response_text == "done"
    await _settle()
    assert queue.empty()


async def test_fast_finish_guard_seeds_initial_progress(
    monkeypatch, fake_redis, event_bus, session_store
):
    queue = await event_bus.register(SID)
    await _seed(session_store, awaited=["task-1"])

    _patch_task_events(
        monkeypatch,
        events_seq=[],  # no live events
        snapshot={"state": "running", "last_status_message": "warming up"},
    )

    await run_task_subscriber(event_bus, session_store, SID, fake_redis)

    (event,) = await _drain(queue, 1)
    assert isinstance(event, TaskProgressEvent)
    assert event.message == "warming up"
    assert await session_store.is_awaiting(SID, "task-1")


async def test_history_limit_constant():
    assert events_service.BACKGROUND_STATUS_HISTORY_LIMIT == 50


# --------------------------------------------------------------------------- #
# E-2 — awaited id restored on failure/cancellation (not lost mid-announce)
# --------------------------------------------------------------------------- #


async def test_announce_restores_id_on_fetch_failure(
    monkeypatch, fake_redis, event_bus, session_store
):
    await event_bus.register(SID)
    await _seed(session_store, awaited=["task-1"])

    async def boom_fetch(redis, conversation_id, task_id):
        raise RuntimeError("redis down mid-fetch")

    monkeypatch.setattr(task_events, "fetch_result", boom_fetch)

    with pytest.raises(RuntimeError):
        await events_service._announce_completion(
            event_bus, session_store, SID, fake_redis, "task-1", "completed", conversation_id="c1", auto_approve=False
        )
    # The id is put back so a reconnecting subscriber's guard can retry (not lost).
    assert await session_store.is_awaiting(SID, "task-1")


async def test_claim_task_is_exactly_once_under_concurrency(session_store):
    """The ``SREM``-as-claim: two concurrent claimants, exactly one winner.

    This is the multi-worker version of what used to be a check-then-discard against
    an in-process set — the guarantee that stops ``task_done`` being announced twice.
    """
    await _seed(session_store, awaited=["task-1"])

    results = await asyncio.gather(
        session_store.claim_task(SID, "task-1"),
        session_store.claim_task(SID, "task-1"),
        session_store.claim_task(SID, "task-1"),
    )
    assert sorted(results) == [False, False, True]
    assert not await session_store.is_awaiting(SID, "task-1")


# --------------------------------------------------------------------------- #
# E-3 — per-connection queue is bounded (drops oldest on overflow)
# --------------------------------------------------------------------------- #


async def test_event_queue_is_bounded_and_drops_oldest(monkeypatch, event_bus):
    monkeypatch.setattr(events_service, "EVENT_QUEUE_MAXSIZE", 2)
    queue = await event_bus.register(SID)

    for i in range(4):
        await event_bus.publish(SID, StatusEvent(turn_id="t", message=f"m{i}"))

    # The relay delivers asynchronously, so wait for all four to have been handled
    # (the queue saturates at 2 and then rotates) rather than assuming.
    await _wait_until(lambda: queue.qsize() == 2)
    await _settle()
    assert queue.qsize() == 2
    # Oldest (m0, m1) were dropped; the two newest remain in order.
    assert [(await queue.get()).message for _ in range(2)] == ["m2", "m3"]


# --------------------------------------------------------------------------- #
# T-4 — reconcile a fast-finished backgrounded task
# --------------------------------------------------------------------------- #


async def test_reconcile_announces_when_already_terminal(
    monkeypatch, fake_redis, event_bus, session_store
):
    queue = await event_bus.register(SID)  # a stream is listening
    await _seed(session_store, awaited=["task-1"])

    _patch_task_events(
        monkeypatch,
        snapshot={"state": "completed"},
        result={"conversation_id": "c1", "status": "answer", "response_text": "bg done"},
    )

    await events_service.reconcile_backgrounded(
        event_bus, session_store, SID, fake_redis, "task-1", auto_approve=False
    )

    (event,) = await _drain(queue, 1)
    assert isinstance(event, TaskDoneEvent) and event.state == "completed"
    assert event.response is not None and event.response.response_text == "bg done"
    assert not await session_store.is_awaiting(SID, "task-1")


async def test_reconcile_is_noop_without_listeners(
    monkeypatch, fake_redis, event_bus, session_store
):
    # No register → PUBSUB NUMSUB reports nobody on the channel, on any worker.
    await _seed(session_store, awaited=["task-1"])

    _patch_task_events(monkeypatch, snapshot={"state": "completed"}, result={"status": "answer"})

    await events_service.reconcile_backgrounded(
        event_bus, session_store, SID, fake_redis, "task-1", auto_approve=False
    )
    # Left awaited so the connect-time fast-finish guard catches it when the SSE opens.
    assert await session_store.is_awaiting(SID, "task-1")


# --------------------------------------------------------------------------- #
# T-5 — one background-task subscriber per session, shared across connections
# --------------------------------------------------------------------------- #


async def test_two_connections_share_one_subscriber(monkeypatch, fake_redis, event_bus, session_store):
    from starlette.requests import Request

    from app.routers import events as events_router
    from app.routers.events import _event_stream

    starts = 0

    async def fake_subscriber(bus_arg, store_arg, sid_arg, redis_arg, *, auto_approve=False):
        nonlocal starts
        starts += 1
        await asyncio.sleep(3600)  # stay alive until cancelled

    monkeypatch.setattr(events_router, "run_task_subscriber", fake_subscriber)

    sid = "sid-multi"
    await _seed(session_store, sid)
    scope = {"type": "http", "method": "GET", "headers": []}

    async def _receive():
        return {"type": "http.request"}

    q1 = await event_bus.register(sid)
    q2 = await event_bus.register(sid)
    s1 = _event_stream(Request(scope, _receive), event_bus, session_store, sid, fake_redis, q1)
    s2 = _event_stream(Request(scope, _receive), event_bus, session_store, sid, fake_redis, q2)
    assert await asyncio.wait_for(s1.__anext__(), 2.0) == ": connected\n\n"
    assert await asyncio.wait_for(s2.__anext__(), 2.0) == ": connected\n\n"
    await asyncio.sleep(0)  # let the created subscriber task run

    assert starts == 1  # only the first connection started the subscriber
    assert event_bus.local_listener_count(sid) == 2

    # Closing one connection leaves the shared subscriber running…
    await s1.aclose()
    assert event_bus.get_task_subscriber(sid) is not None
    # …closing the last tears it down.
    await s2.aclose()
    assert event_bus.get_task_subscriber(sid) is None


# --------------------------------------------------------------------------- #
# E-1 — in-flight status history accumulates (deduped, capped)
# --------------------------------------------------------------------------- #


async def test_on_status_accumulates_deduped_history(event_bus):
    from app.routers.chat import _make_on_status

    queue = await event_bus.register(SID)
    on_status = _make_on_status(event_bus, SID, "turn-x")
    assert on_status is not None

    await on_status("step one")
    await on_status("step one")  # consecutive duplicate → suppressed, no event
    await on_status("step two")

    first = await asyncio.wait_for(queue.get(), 2.0)
    second = await asyncio.wait_for(queue.get(), 2.0)
    assert first.history == ["step one"]
    assert second.history == ["step one", "step two"]
    await _settle()
    assert queue.empty()


# --------------------------------------------------------------------------- #
# GET /api/events — endpoint wiring & frame shaping
# --------------------------------------------------------------------------- #


def test_events_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/events")
    assert resp.status_code == 401


async def test_event_stream_shapes_connect_heartbeat_and_status_frame(
    monkeypatch, fake_redis, event_bus, session_store
):
    """Drive ``_event_stream`` directly: connect comment, then a status frame.

    Avoids ``TestClient.stream`` (which runs the generator on a separate loop, so
    a queue published from the test loop would never be seen). The subscriber is
    stubbed to a no-op so the stream stays alive on real events only.
    """
    from starlette.requests import Request

    from app.routers.events import _event_stream

    async def _empty_subscribe(redis, conversation_id):
        return
        yield  # pragma: no cover - marks this an async generator

    async def _no_snapshot(redis, conversation_id, task_id):
        return None

    monkeypatch.setattr(task_events, "subscribe", _empty_subscribe)
    monkeypatch.setattr(task_events, "node_snapshot", _no_snapshot)

    sid = "sid-evt"
    await _seed(session_store, sid)

    # Minimal ASGI request that never reports disconnected.
    scope = {"type": "http", "method": "GET", "headers": []}

    async def _receive():
        return {"type": "http.request"}

    request = Request(scope, _receive)
    queue = await event_bus.register(sid)

    stream = _event_stream(request, event_bus, session_store, sid, fake_redis, queue)
    first = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert first == ": connected\n\n"

    await event_bus.publish(sid, StatusEvent(turn_id="t1", message="working"))
    frame = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert frame.startswith("event: status\n")
    assert '"message":"working"' in frame
    assert frame.endswith("\n\n")

    await stream.aclose()
