"""Per-session event delivery: Valkey-backed bus + background-task Redis subscriber.

This is the server half of decision **D2** — one long-lived channel per browser
session over which two independent producers push typed events:

- **In-flight turn status.** The chat turn's ``on_status`` closure
  (``routers/chat.py``) calls :meth:`EventBus.publish` with a
  :class:`~app.contract.StatusEvent`. This replaces Chainlit's live ``cl.Step``
  updates.
- **Backgrounded-task progress / completion.** A per-stream Redis subscriber
  (:func:`run_task_subscriber`) ports ``message_handler.run_task_notifier``
  (frontend/components/message_handler.py:432). Chainlit started that loop in
  ``on_chat_start`` bound to the websocket context; here it is owned by the SSE
  connection lifetime — started when the stream opens, cancelled on disconnect.

The :class:`EventBus` is the seam ``routers/chat.py`` already reaches for via
``app.state.event_bus``; the SSE endpoint (``routers/events.py``) drains it. It fans out
over Valkey pub/sub rather than in-process sets, so the worker handling a chat POST and
the worker holding that session's SSE stream need not be the same one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import TypeAdapter

from app import task_events
from app.a2a_client import response_from_payload
from app.contract import ServerEvent, TaskDoneEvent, TaskProgressEvent
from app.redis_client import RedisClient
from app.services.chat import map_response
from app.sessions import SessionStore

logger = logging.getLogger(__name__)

# Parses a JSON pub/sub frame back into the discriminated ``ServerEvent`` union. Built
# once: TypeAdapter construction compiles a validator and is not cheap per message.
_EVENT_ADAPTER: TypeAdapter[ServerEvent] = TypeAdapter(ServerEvent)

# Cap on retained per-task progress lines (ported from
# message_handler.BACKGROUND_STATUS_HISTORY_LIMIT). Bounds memory for a chatty
# background task; only used to trim the dedup history, not surfaced on the wire.
BACKGROUND_STATUS_HISTORY_LIMIT = 50

# Bound on a single connection's event queue (E-3). A stalled-but-not-disconnected
# consumer can't grow it without limit; on overflow the oldest event is dropped.
EVENT_QUEUE_MAXSIZE = 1000

#: What a connection's queue carries: an event, or ``None`` -- the close sentinel
#: :meth:`EventBus._drain` pushes when this worker's relay for the sid dies, telling the
#: SSE stream to end so the browser's ``EventSource`` reconnects through ``register``.
StreamItem = Optional[ServerEvent]


def _channel(sid: str) -> str:
    """Valkey pub/sub channel carrying one session's SSE events."""
    return f"sse:{sid}"


async def _close_pubsub(pubsub: Any, sid: str) -> None:
    """Best-effort teardown of a relay's subscription."""
    for close in (lambda: pubsub.unsubscribe(_channel(sid)), pubsub.aclose):
        try:
            await close()
        except Exception:  # noqa: BLE001 - cleanup only
            pass


@dataclass
class _Relay:
    """One worker's Valkey subscription for a sid, plus the task draining it.

    ``task`` is ``None`` only for the instant between constructing the relay and starting
    its drain task, which needs the relay itself to recognise it is still the current one.
    """

    pubsub: Any
    task: "Optional[asyncio.Task[None]]"


def _put_dropping_oldest(queue: "asyncio.Queue[StreamItem]", item: StreamItem, sid: str) -> None:
    """Enqueue without blocking; on overflow drop the oldest entry to make room.

    A wedged consumer loses old progress rather than growing memory without bound (E-3).
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("event queue full for sid=%s; dropped an event", sid)


# --------------------------------------------------------------------------- #
# Session event bus
# --------------------------------------------------------------------------- #


class EventBus:
    """Per-session event fan-out over Valkey pub/sub (channel ``sse:{sid}``).

    A session (``sid``) may have more than one open stream (e.g. two tabs), and under
    ``uvicorn --workers N`` those tabs may be held by *different worker processes*.
    Publishing therefore goes to Valkey and comes back to each worker that has at least
    one local stream for the sid, which then hands the event to every local queue —
    exactly once per queue. Publishing to a sid nobody is streaming is a no-op: a status
    event fired before the SSE stream connects is simply dropped, matching the "no
    proactive UI when nobody's listening" behavior of the old websocket-bound notifier.

    Two counts, deliberately distinct — conflating them was the trap the old
    ``listener_count`` walked into once this stopped being in-process:

    - :meth:`has_listeners` asks Valkey (``PUBSUB NUMSUB``) and so answers for the whole
      cluster. That is what "would anyone receive this?" means.
    - :meth:`local_listener_count` counts this worker's queues only, which is what the
      per-worker relay and background-task-subscriber teardown must key off.

    The bus also tracks **one** background-task Redis subscriber per sid *per worker*
    (see ``routers/events.py``): the first local connection for a sid starts it, the last
    local one to close cancels it, so N tabs on one worker share a single Task Graph
    subscription rather than each opening its own (T-5). Because every worker with a tab
    for the sid runs its own subscriber, what that subscriber produces must stay on the
    worker: ``task_progress`` goes through :meth:`deliver_local`, never :meth:`publish`,
    or each line would reach every tab once per worker. ``task_done`` *is* published —
    :meth:`SessionStore.claim_task` makes exactly one subscriber cluster-wide announce it.

    Single event loop per worker, so the plain dicts/sets need no lock of their own. The
    one exception is :meth:`register`'s first-subscriber branch, which awaits a
    ``SUBSCRIBE`` in the middle of a check-then-install and so holds ``_register_lock``.
    """

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis
        self._subscribers: dict[str, set[asyncio.Queue[StreamItem]]] = {}
        # One Valkey subscription per sid per worker, feeding every local queue.
        self._relays: dict[str, _Relay] = {}
        self._task_subscribers: dict[str, "asyncio.Task[None]"] = {}
        # Serialises register()'s "no relay yet → SUBSCRIBE → install" so two tabs of one
        # login connecting together (every open tab does, right after a backend restart)
        # can't both see an empty set and both subscribe, orphaning the first relay.
        # Per worker rather than per sid: it is only held across one SUBSCRIBE round trip
        # for a sid's first stream, and a per-sid lock map would need its own cleanup.
        self._register_lock = asyncio.Lock()

    async def publish(self, sid: str, event: ServerEvent) -> None:
        """Broadcast ``event`` to every worker streaming ``sid``.

        Never raises: these events are advisory progress, and a Valkey blip must not
        turn a working chat turn into a 500. The turn's own HTTP response still carries
        the outcome.
        """
        try:
            await self._redis.publish(_channel(sid), event.model_dump_json())
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            logger.warning("failed to publish %s event for sid=%s: %s", event.type, sid, exc)

    async def register(self, sid: str) -> "asyncio.Queue[StreamItem]":
        """Add and return a fresh bounded queue for ``sid``. Pair with :meth:`unregister`.

        The Valkey ``SUBSCRIBE`` for the first local queue is awaited **here**, before
        this returns, rather than left to the relay task to get to. Otherwise an event
        published in the same breath as the stream opening — which is precisely what
        ``_fast_finish_guard`` does — would land before the subscription existed and be
        lost. Propagates on a Valkey failure so the caller can fail the request visibly
        instead of serving a stream that silently delivers nothing.

        The check and the install happen under ``_register_lock``. Adding the queue to the
        set *before* the await would also stop a second relay, but then the second caller
        would return before the first caller's ``SUBSCRIBE`` landed — exactly the lost
        event the paragraph above exists to prevent.
        """
        async with self._register_lock:
            if sid not in self._relays:
                pubsub = self._redis.pubsub()
                try:
                    await pubsub.subscribe(_channel(sid))
                except Exception:
                    await _close_pubsub(pubsub, sid)
                    raise
                relay = _Relay(pubsub=pubsub, task=None)
                relay.task = asyncio.create_task(self._drain(sid, relay))
                self._relays[sid] = relay
            queue: asyncio.Queue[StreamItem] = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
            self._subscribers.setdefault(sid, set()).add(queue)
            return queue

    async def unregister(self, sid: str, queue: "asyncio.Queue[StreamItem]") -> None:
        """Remove a queue; drop the sid's relay once its last local stream closes."""
        subs = self._subscribers.get(sid)
        if not subs:
            return
        subs.discard(queue)
        if subs:
            return
        self._subscribers.pop(sid, None)
        relay = self._relays.pop(sid, None)
        if relay is None:
            return
        if relay.task is not None:
            relay.task.cancel()
            try:
                await relay.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - cleanup only
                pass
        await _close_pubsub(relay.pubsub, sid)

    async def _drain(self, sid: str, relay: _Relay) -> None:
        """Relay this sid's Valkey channel into every local queue until cancelled.

        If the subscription itself dies — ``listen()`` raising after Valkey restarts and
        redis-py's reconnects run out — the relay does not just log and stop. That left
        a finished task installed for the sid: every open stream kept heart-beating and
        never received another event, and a new tab skipped the ``SUBSCRIBE`` because the
        sid looked covered. Instead the relay forgets itself and this sid's local queues,
        and closes each stream (the ``None`` sentinel), so every ``EventSource``
        reconnects and ``register`` subscribes afresh.
        """
        try:
            async for message in relay.pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = _EVENT_ADAPTER.validate_json(message["data"])
                except Exception:  # noqa: BLE001 - a malformed frame must not kill the relay
                    logger.warning("discarding unparseable SSE event for sid=%s", sid)
                    continue
                self.deliver_local(sid, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the streams below
            logger.warning("SSE relay for sid=%s stopped: %s; closing its streams", sid, exc)
            if self._relays.get(sid) is relay:
                del self._relays[sid]
                for queue in self._subscribers.pop(sid, set()):
                    _put_dropping_oldest(queue, None, sid)
            await _close_pubsub(relay.pubsub, sid)

    def deliver_local(self, sid: str, event: ServerEvent) -> None:
        """Hand ``event`` to this worker's queues for ``sid`` only — no Valkey round trip.

        For events this worker produced for its own tabs (``task_progress`` from its
        per-worker task subscriber); see the class docstring for why those must not be
        published.
        """
        for queue in list(self._subscribers.get(sid, ())):
            _put_dropping_oldest(queue, event, sid)

    async def has_listeners(self, sid: str) -> bool:
        """Whether *any* worker currently streams ``sid`` (``PUBSUB NUMSUB``).

        Fails **open** (``True``) on a Valkey error: its only caller,
        :func:`reconcile_backgrounded`, uses it to avoid consuming an awaited task id
        nobody would receive. Answering ``False`` on a blip would leave the id awaited
        for the connect-time guard to pick up, which is also safe — but answering
        ``True`` and attempting the announce is the branch that still delivers if the
        publish succeeds.
        """
        try:
            counts = await self._redis.pubsub_numsub(_channel(sid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("listener check failed for sid=%s (%s); assuming a listener", sid, exc)
            return True
        return any(int(count) > 0 for _channel_name, count in counts)

    def local_listener_count(self, sid: str) -> int:
        """Queues this worker holds for ``sid``. Not cluster-wide — see the class docstring."""
        return len(self._subscribers.get(sid, ()))

    # -- per-session background-task subscriber lifecycle (T-5) -------------- #

    def get_task_subscriber(self, sid: str) -> "Optional[asyncio.Task[None]]":
        return self._task_subscribers.get(sid)

    def set_task_subscriber(self, sid: str, task: "asyncio.Task[None]") -> None:
        self._task_subscribers[sid] = task

    def clear_task_subscriber(self, sid: str) -> None:
        self._task_subscribers.pop(sid, None)


# --------------------------------------------------------------------------- #
# Backgrounded-task completion / progress (ports run_task_notifier)
# --------------------------------------------------------------------------- #


async def _announce_completion(
    bus: EventBus,
    store: SessionStore,
    sid: str,
    redis: RedisClient,
    task_id: str,
    state: str,
    *,
    conversation_id: str,
    auto_approve: bool,
) -> None:
    """Emit the terminal event for a backgrounded task, exactly once.

    Ports ``message_handler._maybe_announce_completion`` (message_handler.py:403).
    Idempotency comes from :meth:`SessionStore.claim_task`: a single ``SREM`` whose
    return count *is* the claim, so exactly one of the subscriber loop, the fast-finish
    guard and the router's reconcile wins — atomically, and across workers. (This used
    to be a check-then-discard against an in-process set, correct only because there was
    one process.) Non-terminal states are ignored.

    ``completed`` → fetch the persisted result and run it through the same
    ``map_response`` the synchronous turn uses (via ``response_from_payload``), so a
    backgrounded answer reaches the frontend identically to an inline one. Any other
    terminal state emits a bodyless :class:`TaskDoneEvent`.

    ``conversation_id`` is the one the caller observed the task under; the result is
    fetched from, and ``map_response``'s session writes are guarded on, that id.
    """
    if not task_events.is_terminal(state):
        return
    if not await store.claim_task(sid, task_id):
        return  # already announced (or never ours)

    # The claim is consumed up front, so restore it on failure or cancellation —
    # otherwise a disconnect mid-``fetch_result`` would consume the id without ever
    # delivering ``task_done``, and a reconnecting subscriber's guard wouldn't find it
    # to retry (E-2).
    try:
        if state != "completed":
            await bus.publish(
                sid,
                TaskDoneEvent(
                    task_id=task_id,
                    state=state,
                    message=f"Background task ended ({state}).",  # E-5
                    response=None,
                ),
            )
            return

        result = await task_events.fetch_result(redis, conversation_id, task_id)
        response = None
        if result:
            # Reuse the turn mapper; the task id doubles as the turn id for this event.
            response = await map_response(
                response_from_payload(result),
                task_id,
                store,
                sid,
                conversation_id=conversation_id,
                auto_approve=auto_approve,
            )
        await bus.publish(
            sid,
            TaskDoneEvent(task_id=task_id, state="completed", response=response),
        )
    except BaseException:  # incl. CancelledError — restore so a reconnect retries
        # Guarded like any awaited-set write: if the session has moved on, the id no
        # longer belongs in its set.
        await store.await_task(sid, task_id, conversation_id=conversation_id)
        raise


async def reconcile_backgrounded(
    bus: EventBus,
    store: SessionStore,
    sid: str,
    redis: RedisClient,
    task_id: str,
    *,
    auto_approve: bool = False,
) -> None:
    """Re-check a just-backgrounded task once, in case it finished before the live
    subscriber saw its terminal event (T-4).

    Ports the per-POST snapshot recovery from ``_handle_backgrounded``
    (message_handler.py:389). The old design ran this guard on the same request that
    detached the task; the new subscriber only guards ids awaited *at connect time*,
    so an id registered after connect (this turn) can slip past the subscribe loop if
    the terminal event lands before ``_map_backgrounded`` registers it.

    Only acts when a stream is actually listening for this sid — on *any* worker, hence
    the ``PUBSUB NUMSUB`` in :meth:`EventBus.has_listeners`; otherwise the id stays
    awaited and the connect-time fast-finish guard catches it when the SSE opens (so
    this never consumes an id that nobody would receive). Idempotent via the
    ``claim_task`` claim in :func:`_announce_completion`.
    """
    if not await bus.has_listeners(sid):
        return
    session = await store.get_or_create(sid)
    if task_id not in session.awaited_task_ids:
        return
    try:
        node = await task_events.node_snapshot(redis, session.conversation_id or "", task_id)
    except Exception:  # a missing/broken snapshot must not fail the turn response
        return
    state = str(node.get("state") or "") if node else ""
    if task_events.is_terminal(state):
        await _announce_completion(
            bus,
            store,
            sid,
            redis,
            task_id,
            state,
            conversation_id=session.conversation_id or "",
            auto_approve=auto_approve,
        )


def _emit_progress(
    bus: EventBus,
    sid: str,
    history: dict[str, list[str]],
    task_id: str,
    message: str,
) -> None:
    """Deliver a non-terminal progress line, de-duplicating consecutive repeats.

    Ports ``_update_background_progress`` (message_handler.py:326): blank messages
    and immediate repeats are dropped, and the retained history is capped at
    :data:`BACKGROUND_STATUS_HISTORY_LIMIT`.

    Delivered to this worker's queues only (:meth:`EventBus.deliver_local`): every
    worker with a tab for the sid runs its own subscriber and so produces its own copy
    of each line, and publishing those would hand every tab one copy per worker.
    ``history`` belongs to that per-worker subscriber, so dedup is per worker too.
    """
    text = str(message or "").strip()
    if not text:
        return
    entries = history.setdefault(task_id, [])
    if entries and entries[-1] == text:
        return
    entries.append(text)
    del entries[:-BACKGROUND_STATUS_HISTORY_LIMIT]
    bus.deliver_local(sid, TaskProgressEvent(task_id=task_id, message=text))


async def _fast_finish_guard(
    bus: EventBus,
    store: SessionStore,
    sid: str,
    redis: RedisClient,
    history: dict[str, list[str]],
    *,
    auto_approve: bool,
) -> None:
    """Catch tasks that finished before the subscriber saw their event.

    Ports the guard from ``_handle_backgrounded`` (message_handler.py:389): detach
    normally precedes completion, but a very fast task can reach a terminal node
    state in the window between the ``backgrounded`` POST returning and this
    subscriber's ``SUBSCRIBE`` landing. For each id the session is already awaiting
    we read the persisted node once; terminal → announce now (the ``claim_task`` in
    :func:`_announce_completion` stops the live loop double-announcing), otherwise
    seed the initial progress line.
    """
    session = await store.get_or_create(sid)
    conversation_id = session.conversation_id or ""
    for task_id in sorted(session.awaited_task_ids):
        try:
            node = await task_events.node_snapshot(redis, conversation_id, task_id)
        except Exception:  # a missing/broken snapshot must not abort the stream
            node = None
        state = str(node.get("state") or "") if node else ""
        if task_events.is_terminal(state):
            await _announce_completion(
                bus,
                store,
                sid,
                redis,
                task_id,
                state,
                conversation_id=conversation_id,
                auto_approve=auto_approve,
            )
        elif node:
            _emit_progress(bus, sid, history, task_id, str(node.get("last_status_message") or ""))


async def run_task_subscriber(
    bus: EventBus,
    store: SessionStore,
    sid: str,
    redis: RedisClient,
    *,
    auto_approve: bool = False,
) -> None:
    """Owned by one SSE connection: relay this session's backgrounded-task events.

    Ports ``message_handler.run_task_notifier`` (message_handler.py:432) minus the
    Chainlit websocket-context binding — here the events go onto the
    :class:`EventBus` for the SSE stream to drain. Runs the fast-finish guard once,
    then loops over ``task_events.subscribe`` until cancelled on disconnect.

    The awaited-id set is re-read from the store on each event rather than captured:
    ``POST /api/messages`` can register a new backgrounded task mid-stream, and after
    this stopped being one shared in-process object the only way to see that is to ask.

    Only **root** events for tasks the session is awaiting are acted on: child-node
    updates percolate to the root, so filtering on ``task_id == root_task_id`` keeps
    one specialist's status from surfacing twice (message_handler.py:457).
    """
    history: dict[str, list[str]] = {}
    await _fast_finish_guard(bus, store, sid, redis, history, auto_approve=auto_approve)

    session = await store.get_or_create(sid)
    conversation_id = session.conversation_id or ""
    if not conversation_id:
        return

    try:
        async for event in task_events.subscribe(redis, conversation_id):
            task_id = str(event.get("task_id") or "")
            root_task_id = str(event.get("root_task_id") or task_id)
            state = str(event.get("state") or "")
            if not task_id or task_id != root_task_id:
                continue
            if not await store.is_awaiting(sid, root_task_id):
                continue
            try:
                if task_events.is_terminal(state):
                    await _announce_completion(
                        bus,
                        store,
                        sid,
                        redis,
                        root_task_id,
                        state,
                        conversation_id=conversation_id,
                        auto_approve=auto_approve,
                    )
                else:
                    _emit_progress(bus, sid, history, root_task_id, str(event.get("message") or ""))
            except Exception as exc:
                logger.warning("Failed to relay background task %s: %s", root_task_id, exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a dead subscription shouldn't crash the SSE stream
        logger.warning("Task subscriber for %s stopped: %s", conversation_id, exc)
