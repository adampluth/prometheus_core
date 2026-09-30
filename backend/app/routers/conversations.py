"""Conversation history: list, fetch, rename, soft-delete, and resume.

``GET /api/conversations/recent`` replaces the Chainlit ``/history`` slash command
(``_handle_conversation_history``, frontend/main.py:107): returns recent conversation
summaries as JSON instead of a markdown ``cl.Message``. Slash-command parsing is now
a frontend concern.

The rest of this router is new (chat-history feature): ``GET /{id}`` fetches one
past conversation's transcript, ``PATCH``/``DELETE`` let a user rename or soft-delete
it, and ``POST /{id}/resume`` rebinds the caller's session to it so the next chat
turn continues that conversation rather than starting a fresh one.

``ConversationStateClient`` (app/conversation_state.py) uses the **synchronous**
redis client, so every call is offloaded to a worker thread with
``asyncio.to_thread`` to keep the event loop free. When Redis is unreachable,
``recent`` degrades to ``{available: false, conversations: []}`` with a 200; the
per-conversation endpoints 404 instead, since there's nothing to act on.

Every per-conversation endpoint checks ownership (``user:convs:{user}`` via
``ConversationStateClient.owns``) and returns **404** — not 403 — on a miss, so a
guess at another user's conversation id can't even confirm the id exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status as http_status

from app.config import settings
from app.contract import (
    ConversationDetailResponse,
    ConversationHistoryResponse,
    ConversationMessage,
    ConversationRenameRequest,
    ConversationResumeRequest,
    ConversationSummary,
)
from app.conversation_state import ConversationStateClient
from app.security import TokenClaims, current_claims
from app.services.artifacts import attribute_artifacts, shape_artifacts
from app.sessions import get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["conversations"])


# One client reused across requests (AU-7): ConversationStateClient caches its Redis
# connection, so a shared instance avoids a fresh connect + ping per call. Keyed on the
# class object (not id()) so tests monkeypatching ``ConversationStateClient`` get their
# own instance without id-reuse hazards, while the class is kept alive as the cache key.
_client_cache: dict = {}


def _client() -> ConversationStateClient:
    cls = ConversationStateClient
    inst = _client_cache.get(cls)
    if inst is None:
        inst = cls(settings.redis_url)
        _client_cache[cls] = inst
    return inst


def _fetch_recent(limit: int, user_id: str) -> tuple[bool, list[dict]]:
    """Blocking Redis read, run in a worker thread.

    Returns ``(available, rows)``: ``available`` distinguishes "Redis down" from
    "Redis up but empty" (both yield an empty ``rows``). ``user_id`` scopes the
    result to the caller's own conversations (see ``ConversationStateClient``).
    """
    client = _client()
    if not client.available:
        return False, []
    return True, client.get_conversation_history(limit=limit, user_id=user_id)


@router.get("/conversations/recent", response_model=ConversationHistoryResponse)
async def get_recent_conversations(
    limit: int = Query(20, ge=1, le=100),
    claims: TokenClaims = Depends(current_claims),
) -> ConversationHistoryResponse:
    available, rows = await asyncio.to_thread(_fetch_recent, limit, claims.sub)
    return ConversationHistoryResponse(
        available=available,
        conversations=[ConversationSummary(**row) for row in rows],
    )


def _fetch_detail(conv_id: str, user_id: str) -> tuple[bool, Any]:
    """Blocking ownership check + detail assembly, run in a worker thread.

    Returns ``(found, (detail, raw_artifacts) or None)``: ``found`` is ``False`` when
    Redis is down, the conversation doesn't exist, or it isn't owned by *user_id* —
    the caller maps that uniformly to 404 so a non-owner can't distinguish "doesn't
    exist" from "not yours". The detail carries no artifacts yet; :func:`_load_detail`
    attaches them.
    """
    client = _client()
    if not client.available or not client.owns(user_id, conv_id):
        return False, None
    conv = client.get_conversation(conv_id) or {}
    meta = client.get_meta(conv_id)
    detail = ConversationDetailResponse(
        conversation_id=conv_id,
        title=meta.get("title_override") or conv.get("title") or conv.get("user_query", ""),
        profile=str(conv.get("profile") or ""),
        messages=[
            ConversationMessage(role=m["role"], content=m["content"], timestamp=m["timestamp"])
            for m in client.get_transcript(conv_id)
        ],
    )
    return True, (detail, client.get_artifacts(conv_id))


async def _load_detail(conv_id: str, user_id: str) -> tuple[ConversationDetailResponse, list[dict]]:
    """Fetch a conversation the caller owns (404 otherwise), its artifacts shaped and
    attached to the replies of the turns that produced them.

    Shaping is metadata only — previews (e.g. mesh conversion) are built lazily when
    their viewer scrolls into view (``ArtifactMeta.preview_url``), so reopening a
    conversation is cheap no matter what it holds. Returns the raw catalog too, for
    resume to put on the session.
    """
    found, loaded = await asyncio.to_thread(_fetch_detail, conv_id, user_id)
    if not found:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    detail, raw_artifacts = cast(tuple[ConversationDetailResponse, list[dict]], loaded)
    attribute_artifacts(detail.messages, shape_artifacts(raw_artifacts, conv_id))
    return detail, raw_artifacts


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation_detail(
    conversation_id: str,
    claims: TokenClaims = Depends(current_claims),
) -> ConversationDetailResponse:
    detail, _ = await _load_detail(conversation_id, claims.sub)
    return detail


def _rename(conv_id: str, user_id: str, title: str) -> bool:
    client = _client()
    if not client.available or not client.owns(user_id, conv_id):
        return False
    return client.set_title_override(conv_id, title)


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: str,
    body: ConversationRenameRequest,
    claims: TokenClaims = Depends(current_claims),
) -> ConversationSummary:
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Title must not be empty")
    ok = await asyncio.to_thread(_rename, conversation_id, claims.sub, title)
    if not ok:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return ConversationSummary(conversation_id=conversation_id, title=title)


def _soft_delete(conv_id: str, user_id: str) -> bool:
    client = _client()
    if not client.available or not client.owns(user_id, conv_id):
        return False
    return client.set_hidden(conv_id, True)


@router.delete("/conversations/{conversation_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    claims: TokenClaims = Depends(current_claims),
) -> None:
    """Soft delete: hides the conversation from ``recent``. The underlying
    ``conversation:{id}`` document (and its artifacts) are left intact."""
    ok = await asyncio.to_thread(_soft_delete, conversation_id, claims.sub)
    if not ok:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Conversation not found")


@router.post("/conversations/{conversation_id}/resume", response_model=ConversationDetailResponse)
async def resume_conversation(
    conversation_id: str,
    request: Request,
    body: Optional[ConversationResumeRequest] = Body(default=None),
    claims: TokenClaims = Depends(current_claims),
) -> ConversationDetailResponse:
    """Rebind the caller's session to a past conversation so the next chat turn
    continues it. Returns its transcript (artifacts attached per turn) in the same
    call so the frontend can render history and resume in one round-trip.

    Loading is metadata only (previews are built lazily), so there is nothing slow to
    order the rebind around; the rebind itself is ordered by ``bind_seq`` (see
    ``SessionStore.reset``), so a newer click always wins regardless of which request
    finishes last."""
    detail, raw_artifacts = await _load_detail(conversation_id, claims.sub)

    store = get_store(request)
    # Resuming the conversation the session is already on (the reload path) keeps the
    # awaited-task set, so a task backgrounded before the reload still streams its
    # `task_progress` / `task_done` into the resumed session -- the ids live in Valkey
    # rather than in a process-local SessionState. Switching to a *different*
    # conversation clears it: those ids only resolve against the conversation that
    # produced them. SessionStore.reset decides which, atomically. Either way the
    # history sidebar marks a conversation pending while `agent:status:{id}` is live,
    # and the result lands in the transcript.
    #
    # The raw catalog goes on the session because the next turn sends it to the
    # client-agent. If a newer rebind already won, this is a no-op -- the response is
    # still returned and the frontend discards it as superseded.
    await store.reset(
        claims.sid,
        conversation_id,
        artifacts=raw_artifacts,
        bind_seq=body.bind_seq if body else None,
    )

    detail.auto_approve_steps = settings.auto_approve_steps
    return detail
