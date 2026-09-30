"""Async Redis pub/sub helpers for background-task notifications.

When the client-agent detaches a slow turn it returns a ``backgrounded`` status
and keeps the orchestration running; on completion it publishes a state-change
event on ``tasks:events:{conversation_id}`` and persists the final response under
``tasks:result:{conversation_id}:{task_id}``. The frontend subscribes here and
renders the result out-of-band when the task finishes.

Uses ``redis.asyncio`` so the listener never blocks the service event loop. Every
function takes the process-wide client from :mod:`app.redis_client` rather than a URL:
these used to build a fresh client per call and ``aclose()`` it in ``finally``, which
cost a TCP connect and teardown on every round trip.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

from app.redis_client import RedisClient

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})


def events_channel(conversation_id: str) -> str:
    return f"tasks:events:{conversation_id}"


def _node_key(conversation_id: str, task_id: str) -> str:
    return f"tasks:node:{conversation_id}:{task_id}"


def _result_key(conversation_id: str, task_id: str) -> str:
    return f"tasks:result:{conversation_id}:{task_id}"


def is_terminal(state: Optional[str]) -> bool:
    return str(state or "") in TERMINAL_STATES


async def _get_json(client: Any, key: str) -> Optional[Dict[str, Any]]:
    try:
        data = await client.get(key)
    except Exception as exc:
        logger.warning("[TASKEVENTS] get %s failed: %s", key, exc)
        return None
    if not data:
        return None
    try:
        parsed: Dict[str, Any] = json.loads(data)
        return parsed
    except Exception:
        return None


async def fetch_result(redis: RedisClient, conversation_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the persisted final response payload for a completed task."""
    return await _get_json(redis, _result_key(conversation_id, task_id))


async def node_snapshot(
    redis: RedisClient,
    conversation_id: str,
    task_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the current persisted Task Graph node, or ``None``.

    Used as a subscribe-race guard: a task that reached a terminal state between
    detach and subscribe won't emit another event, so callers check the node
    directly once on startup. The latest status message also seeds the live
    background-progress display.
    """
    return await _get_json(redis, _node_key(conversation_id, task_id))


async def node_state(redis: RedisClient, conversation_id: str, task_id: str) -> Optional[str]:
    """Return only the current persisted state of a Task Graph node."""
    node = await node_snapshot(redis, conversation_id, task_id)
    return str(node.get("state")) if node else None


async def subscribe(redis: RedisClient, conversation_id: str) -> AsyncIterator[Dict[str, Any]]:
    """Yield Task Graph events for a conversation until the consumer is cancelled.

    Each yielded value is a parsed event dict published on
    ``tasks:events:{conversation_id}`` (``{type, task_id, state, …}``). A Valkey failure
    propagates to the caller, which logs and degrades to "no proactive notifications"
    rather than crashing the SSE stream (see ``services/events.run_task_subscriber``).

    The single long-lived subscriber per session is started when the session's
    SSE events stream is opened and cancelled when it closes. ``pubsub()`` takes its own
    connection from the shared pool, so this does not block other callers of the client.
    """
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(events_channel(conversation_id))
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                event = json.loads(msg["data"])
            except Exception:
                continue
            if isinstance(event, dict):
                yield event
    finally:
        # The client itself is process-wide and stays open; only this subscription
        # (and the connection it borrowed from the pool) is released here.
        for close in (
            lambda: pubsub.unsubscribe(events_channel(conversation_id)),
            pubsub.aclose,
        ):
            try:
                await close()
            except Exception:
                pass
