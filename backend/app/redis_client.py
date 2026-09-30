"""The backend's one ``redis.asyncio`` client, shared by every cross-worker subsystem.

Three things the backend cannot keep in process memory once it runs more than one
worker all live in the same Valkey/Redis instance:

- :mod:`app.sessions` — per-login session state (``session:{sid}``)
- :class:`app.services.events.EventBus` — SSE fan-out (``sse:{sid}`` pub/sub)
- :mod:`app.security` — logout revocation (``revoked:sid:{sid}``)
- :mod:`app.task_events` — background-task progress/results published by the client-agent

They share **one** connection pool, created in the ``app/main.py`` lifespan and reachable
as ``request.app.state.redis`` (or via :func:`get_redis` as a FastAPI dependency).

This replaces two earlier styles, neither of which is usable per-request: the synchronous
:class:`app.conversation_state.ConversationStateClient` wrapped in ``asyncio.to_thread``
(still fine for the occasional history read it owns), and :mod:`app.task_events`, which
used to build a fresh client per call and ``aclose()`` it in ``finally`` — a TCP connect
and teardown on every Redis round trip.

``decode_responses=True`` throughout: every value the backend stores is text (JSON or a
plain scalar), so callers never see ``bytes``.
"""

from __future__ import annotations

from typing import cast

import redis.asyncio as aioredis
from fastapi import Request

from app.config import settings

RedisClient = aioredis.Redis


def create_client(url: str | None = None) -> RedisClient:
    """Build a pooled client for ``url`` (default: the configured Valkey).

    ``settings.redis_url`` is already the single resolution point for the URL and
    ``REDIS_PASSWORD`` (see ``config._redis_url``), so nothing else derives it.
    """
    return cast(RedisClient, aioredis.from_url(url or settings.redis_url, decode_responses=True))


def get_redis(request: Request) -> RedisClient:
    """Dependency/helper: the shared client off app state.

    Tests that drive a router through ``TestClient`` set ``app.state.redis`` to a
    ``fakeredis.aioredis.FakeRedis`` the same way they set ``app.state.session_store``.
    """
    return cast(RedisClient, request.app.state.redis)
