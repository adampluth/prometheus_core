"""Per-session event delivery: in-process bus + background-task Redis subscriber.

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
``app.state.event_bus``; the SSE endpoint (``routers/events.py``) drains it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app import task_events
from app.a2a_client import response_from_payload
from app.contract import ServerEvent, TaskDoneEvent, TaskProgressEvent
from app.services.chat import map_response
from app.sessions import SessionState

logger = logging.getLogger(__name__)

# Cap on retained per-task progress lines (ported from
# message_handler.BACKGROUND_STATUS_HISTORY_LIMIT). Bounds memory for a chatty
# background task; only used to trim the dedup history, not surfaced on the wire.
BACKGROUND_STATUS_HISTORY_LIMIT = 50

# Bound on a single connection's event queue (E-3). A stalled-but-not-disconnected
# consumer can't grow it without limit; on overflow the oldest event is dropped.
EVENT_QUEUE_MAXSIZE = 1000


# --------------------------------------------------------------------------- #
# In-process event bus
# --------------------------------------------------------------------------- #


class EventBus:
    """Fan-out registry of per-session ``asyncio.Queue`` subscribers.

    A session (``sid``) may have more than one open stream (e.g. two tabs); every
    live queue for the sid receives each published event exactly once. Publishing to
    a sid with no subscribers is a no-op — a status event fired before the SSE stream
    connects is simply dropped, matching the "no proactive UI when nobody's listening"
    behavior of the old websocket-bound notifier.

    The bus also tracks **one** background-task Redis subscriber per sid (see
    ``routers/events.py``): the first connection for a sid starts it, the last to
    close cancels it, so N tabs share a single Redis subscription rather than each
    opening its own (T-5) — which otherwise duplicated ``task_progress`` events.

    Single event loop, so the plain dicts/sets need no lock.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[ServerEvent]]] = {}
        self._task_subscribers: dict[str, "asyncio.Task[None]"] = {}

    async def publish(self, sid: str, event: ServerEvent) -> None:
        for queue in list(self._subscribers.get(sid, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest to make room; a wedged consumer loses old progress
                # rather than growing memory without bound (E-3).
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("event queue full for sid=%s; dropped an event", sid)

    def register(self, sid: str) -> "asyncio.Queue[ServerEvent]":
        """Add and return a fresh bounded queue for ``sid``. Pair with :meth:`unregister`."""
        queue: asyncio.Queue[ServerEvent] = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        self._subscribers.setdefault(sid, set()).add(queue)
        return queue

    def unregister(self, sid: str, queue: "asyncio.Queue[ServerEvent]") -> None:
        """Remove a queue; drop the sid entry once its last stream closes."""
        subs = self._subscribers.get(sid)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(sid, None)

    def has_listeners(self, sid: str) -> bool:
        return bool(self._subscribers.get(sid))

    def listener_count(self, sid: str) -> int:
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
    session: SessionState,
    redis_url: str,
    task_id: str,
    state: str,
    *,
    auto_approve: bool,
) -> None:
    """Emit the terminal event for a backgrounded task, exactly once.

    Ports ``message_handler._maybe_announce_completion`` (message_handler.py:403).
    Idempotency comes from the session's ``awaited_task_ids`` set: the first caller
    to consume an id wins, so the subscriber loop and the fast-finish guard can't
    both announce the same task. Non-terminal states are ignored.

    ``completed`` → fetch the persisted result and run it through the same
    ``map_response`` the synchronous turn uses (via ``response_from_payload``), so a
    backgrounded answer reaches the frontend identically to an inline one. Any other
    terminal state emits a bodyless :class:`TaskDoneEvent`.
    """
    if not task_events.is_terminal(state):
        return
    if task_id not in session.awaited_task_ids:
        return  # already announced (or never ours)

    # Claim the id up front so a concurrent caller (fast-finish guard vs. subscribe
    # loop vs. router reconcile) can't double-announce. But restore it on failure or
    # cancellation — otherwise a disconnect mid-``fetch_result`` would consume the id
    # without ever delivering ``task_done``, and a reconnecting subscriber's guard
    # wouldn't find it to retry (E-2).
    session.awaited_task_ids.discard(task_id)
    try:
        if state != "completed":
            await bus.publish(
                session.sid,
                TaskDoneEvent(
                    task_id=task_id,
                    state=state,
                    message=f"Background task ended ({state}).",  # E-5
                    response=None,
                ),
            )
            return

        conversation_id = session.conversation_id or ""
        result = await task_events.fetch_result(redis_url, conversation_id, task_id)
        response = None
        if result:
            # Reuse the turn mapper; the task id doubles as the turn id for this event.
            response = map_response(
                response_from_payload(result),
                task_id,
                session,
                conversation_id=conversation_id,
                auto_approve=auto_approve,
            )
        await bus.publish(
            session.sid,
            TaskDoneEvent(task_id=task_id, state="completed", response=response),
        )
    except BaseException:  # incl. CancelledError — restore so a reconnect retries
        session.awaited_task_ids.add(task_id)
        raise


async def reconcile_backgrounded(
    bus: EventBus,
    session: SessionState,
    redis_url: str,
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
    the terminal event lands before ``_map_backgrounded`` adds it to ``awaited_task_ids``.

    Only acts when a stream is actually listening for this sid; otherwise the id stays
    awaited and the connect-time fast-finish guard catches it when the SSE opens (so
    this never consumes an id that nobody would receive). Idempotent via the
    awaited-id claim in :func:`_announce_completion`.
    """
    if not bus.has_listeners(session.sid):
        return
    if task_id not in session.awaited_task_ids:
        return
    try:
        node = await task_events.node_snapshot(redis_url, session.conversation_id or "", task_id)
    except Exception:  # a missing/broken snapshot must not fail the turn response
        return
    state = str(node.get("state") or "") if node else ""
    if task_events.is_terminal(state):
        await _announce_completion(bus, session, redis_url, task_id, state, auto_approve=auto_approve)


async def _emit_progress(
    bus: EventBus,
    session: SessionState,
    history: dict[str, list[str]],
    task_id: str,
    message: str,
) -> None:
    """Publish a non-terminal progress line, de-duplicating consecutive repeats.

    Ports ``_update_background_progress`` (message_handler.py:326): blank messages
    and immediate repeats are dropped, and the retained history is capped at
    :data:`BACKGROUND_STATUS_HISTORY_LIMIT`.
    """
    text = str(message or "").strip()
    if not text:
        return
    entries = history.setdefault(task_id, [])
    if entries and entries[-1] == text:
        return
    entries.append(text)
    del entries[:-BACKGROUND_STATUS_HISTORY_LIMIT]
    await bus.publish(session.sid, TaskProgressEvent(task_id=task_id, message=text))


async def _fast_finish_guard(
    bus: EventBus,
    session: SessionState,
    redis_url: str,
    history: dict[str, list[str]],
    *,
    auto_approve: bool,
) -> None:
    """Catch tasks that finished before the subscriber saw their event.

    Ports the guard from ``_handle_backgrounded`` (message_handler.py:389): detach
    normally precedes completion, but a very fast task can reach a terminal node
    state in the window between the ``backgrounded`` POST returning and this
    subscriber's ``SUBSCRIBE`` landing. For each id the session is already awaiting
    we read the persisted node once; terminal → announce now (consuming the id, so
    the live loop can't double-announce), otherwise seed the initial progress line.
    """
    for task_id in list(session.awaited_task_ids):
        try:
            node = await task_events.node_snapshot(redis_url, session.conversation_id or "", task_id)
        except Exception:  # a missing/broken snapshot must not abort the stream
            node = None
        state = str(node.get("state") or "") if node else ""
        if task_events.is_terminal(state):
            await _announce_completion(
                bus, session, redis_url, task_id, state, auto_approve=auto_approve
            )
        elif node:
            await _emit_progress(
                bus, session, history, task_id, str(node.get("last_status_message") or "")
            )


async def run_task_subscriber(
    bus: EventBus,
    session: SessionState,
    redis_url: str,
    *,
    auto_approve: bool = False,
) -> None:
    """Owned by one SSE connection: relay this session's backgrounded-task events.

    Ports ``message_handler.run_task_notifier`` (message_handler.py:432) minus the
    Chainlit websocket-context binding — here the events go onto the in-process
    :class:`EventBus` for the SSE stream to drain. Runs the fast-finish guard once,
    then loops over ``task_events.subscribe`` until cancelled on disconnect.

    Only **root** events for tasks the session is awaiting are acted on: child-node
    updates percolate to the root, so filtering on ``task_id == root_task_id`` keeps
    one specialist's status from surfacing twice (message_handler.py:457).
    """
    history: dict[str, list[str]] = {}
    await _fast_finish_guard(bus, session, redis_url, history, auto_approve=auto_approve)

    conversation_id = session.conversation_id or ""
    if not conversation_id:
        return

    try:
        async for event in task_events.subscribe(redis_url, conversation_id):
            task_id = str(event.get("task_id") or "")
            root_task_id = str(event.get("root_task_id") or task_id)
            state = str(event.get("state") or "")
            if not task_id or task_id != root_task_id or root_task_id not in session.awaited_task_ids:
                continue
            try:
                if task_events.is_terminal(state):
                    await _announce_completion(
                        bus, session, redis_url, root_task_id, state, auto_approve=auto_approve
                    )
                else:
                    await _emit_progress(
                        bus, session, history, root_task_id, str(event.get("message") or "")
                    )
            except Exception as exc:
                logger.warning("Failed to relay background task %s: %s", root_task_id, exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a dead subscription shouldn't crash the SSE stream
        logger.warning("Task subscriber for %s stopped: %s", conversation_id, exc)
