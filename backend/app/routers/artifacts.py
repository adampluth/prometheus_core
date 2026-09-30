"""Artifact content, artifact previews, and preview files.

``GET /api/conversations/{cid}/artifacts/{aid}/content`` — resolve + stream bytes (D3).

Auth-required and conversation-scoped. Checks the caller may read the conversation
(owns it, or their session is bound to it — see :func:`find_owned_artifact`; 404 —
not 403 — on a miss, like the history routes), looks
the artifact up in that conversation's Redis catalog (``artifacts:conv:{id}:by_id``,
written by the agents' ``ArtifactRegistry``), resolves its URI to local bytes via
``uri_resolver.resolve_to_local_path`` (``file://`` / ``http(s)://`` / ``deeplynx://``),
and streams the file with the correct ``Content-Type`` and an ``inline`` disposition.
For inline-only artifacts (``json_body`` / text, no URI) it serializes the content
via :func:`app.services.artifacts.artifact_download_payload`.

The lookup deliberately does **not** go through the in-memory session: the session
is keyed by the cookie's ``sid``, shared by every tab, and rebound by "New chat" and
resume — none of which should change whether an artifact the user owns is fetchable. (``ArtifactMeta.content_url`` carries the
route, so the frontend never builds it.)

Consolidates Chainlit's ``_render_pdf_artifact`` / ``_render_remote_file_artifact`` /
``_render_file_artifact`` / ``_render_download_artifact`` into one endpoint; the React
viewers fetch bytes from here instead of the server building elements (task 07).

``GET /api/conversations/{cid}/artifacts/{aid}/preview`` — the viewer props for an
artifact that needs a processed rendition (same access check), building it on first
request with whichever previewer applies (``services/previews``). The frontend calls it
only once the viewer scrolls into view (``ArtifactMeta.preview_url``). ``GET
/api/previews/{key}/{filename}`` then serves the preview's files. Nothing here knows
what kind of artifact a preview is for.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from app import uri_resolver
from app.config import settings
from app.conversation_state import ConversationStateClient
from app.security import TokenClaims, current_claims
from app.services.artifacts import artifact_download_payload, classify_render_hint
from app.services.previews import PreviewFailed, PreviewUnavailable, PreviewUnreadable
from app.services.previews.registry import previewer_for
from app.services.previews.service import preview_props, preview_root
from app.sessions import get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["artifacts"])

# An artifact id names immutable content (registry UUID, or a content hash for id-less
# artifacts), so the browser may keep it: reopening a conversation re-renders its
# images/PDFs without re-downloading them from DeepLynx through us.
_CACHE_HEADERS = {"Cache-Control": "private, max-age=604800, immutable"}

# Render hints served as a browser download rather than viewed inline.
_DOWNLOAD_HINTS = {"file", "unknown"}

# One safe path segment (no separators; a leading dot additionally rejected below, which
# also rules out `..` and keeps the cache's `.build-*` scratch dirs unreachable). Guards
# the key/filename the browser sends to the preview file route.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# One client reused across requests (see routers/conversations.py for the rationale).
_client_cache: dict = {}


def _client() -> ConversationStateClient:
    cls = ConversationStateClient
    inst = _client_cache.get(cls)
    if inst is None:
        inst = cls(settings.redis_url)
        _client_cache[cls] = inst
    return inst


def _lookup(conversation_id: str, artifact_id: str, user_id: str, bound: bool) -> Optional[dict[str, Any]]:
    """Blocking ownership check + catalog read, run in a worker thread.

    ``None`` when Redis is down, the caller may not read the conversation, or the
    artifact isn't in its catalog — all mapped to one 404 so a non-owner can't
    probe which ids exist.
    """
    client = _client()
    if not client.available:
        return None
    if not bound and not client.owns(user_id, conversation_id):
        return None
    return client.get_artifact(conversation_id, artifact_id)


async def find_owned_artifact(
    conversation_id: str, artifact_id: str, request: Request, claims: TokenClaims
) -> dict[str, Any]:
    """The raw catalog dict for an artifact the caller may read, or a 404.

    Readable means the caller owns the conversation (``user:convs:{user}``) **or**
    their session is currently bound to it. The latter covers deployments without
    identity signing keys, where the client-agent never learns the user and so never
    records ownership; it is sound because a session's ``conversation_id`` is only
    ever server-minted (``POST /api/sessions``) or set by an ownership-checked resume.
    """
    session = await get_store(request).get(claims.sid)
    bound = session is not None and session.conversation_id == conversation_id
    artifact = await asyncio.to_thread(_lookup, conversation_id, artifact_id, claims.sub, bound)
    if artifact is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return artifact


def _download_filename(name: str, extension: str) -> str:
    """Ensure a safe basename carrying ``extension`` (ports ``_download_filename``)."""
    filename = Path(str(name or "artifact")).name or "artifact"
    if extension and not Path(filename).suffix:
        filename = f"{filename}{extension}"
    return filename


def _content_disposition(filename: str, disposition: str) -> dict[str, str]:
    """Build a safe ``Content-Disposition`` header (A-5/A-6).

    ``disposition`` is ``inline`` for viewer/browser-renderable content and
    ``attachment`` for downloads. The filename is emitted both as a sanitized ASCII
    ``filename`` and an RFC 5987 ``filename*``, so a quote/backslash/newline/unicode in
    the artifact name can't break out of or inject into the header.
    """
    ascii_name = "".join(c for c in filename if c.isprintable() and c not in '"\\')
    ascii_name = ascii_name.encode("ascii", "ignore").decode() or "artifact"
    encoded = quote(filename, safe="")
    return {"Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"}


@router.get("/conversations/{conversation_id}/artifacts/{artifact_id}/content")
async def get_artifact_content(
    conversation_id: str,
    artifact_id: str,
    request: Request,
    claims: TokenClaims = Depends(current_claims),
) -> Response:
    artifact = await find_owned_artifact(conversation_id, artifact_id, request, claims)

    name = str(artifact.get("name") or "artifact")
    mime_type = artifact.get("mime_type") or ""
    uri = artifact.get("uri") or ""
    # Downloads (file/unknown) get `attachment`; viewer/browser-renderable get `inline`.
    disposition = "attachment" if classify_render_hint(artifact) in _DOWNLOAD_HINTS else "inline"

    # 1. Resolvable URI → stream the local/downloaded file.
    if uri:
        # Per-request scratch dir so downloaded bytes don't accumulate for the service
        # lifetime; a BackgroundTask removes it after the response is sent (A-3). A
        # ``file://`` source resolves outside this dir, so cleanup never touches it.
        cache_dir = Path(tempfile.mkdtemp(prefix="chat-artifact-"))
        local_path = await uri_resolver.resolve_to_local_path(
            uri,
            cache_dir=cache_dir,
            artifact=artifact,
            artifact_id=artifact_id,
        )
        if local_path is None or not local_path.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not resolve artifact {artifact_id!r} from {uri!r}.",
            )
        effective_mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(
            path=str(local_path),
            media_type=effective_mime,
            headers={
                **_CACHE_HEADERS,
                **_content_disposition(_download_filename(name, Path(local_path).suffix), disposition),
            },
            background=BackgroundTask(shutil.rmtree, str(cache_dir), ignore_errors=True),
        )

    # 2. Inline-only content (json_body / text) → serialize and stream.
    payload = artifact_download_payload(artifact, mime_type)
    if payload is not None:
        content, extension, effective_mime = payload
        filename = _download_filename(name, extension)
        return Response(
            content=content,
            media_type=effective_mime,
            headers={**_CACHE_HEADERS, **_content_disposition(filename, disposition)},
        )

    # 3. In catalog but nothing servable.
    raise HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND,
        detail=f"Artifact {artifact_id!r} has no downloadable content.",
    )


@router.get("/conversations/{conversation_id}/artifacts/{artifact_id}/preview")
async def get_artifact_preview(
    conversation_id: str,
    artifact_id: str,
    request: Request,
    claims: TokenClaims = Depends(current_claims),
) -> Response:
    """The viewer props for an artifact's preview, plus ``baseUrl`` for its files.

    The first request for a source builds the preview (seconds to minutes for a large
    mesh); after that it's a disk read (see ``services/previews/service.py``). Failures
    return an error the viewer shows next to a download link: 502 when the source
    can't be fetched, 422 when the previewer can't read it, 500 otherwise.
    """
    artifact = await find_owned_artifact(conversation_id, artifact_id, request, claims)
    previewer = previewer_for(artifact, classify_render_hint(artifact))
    if previewer is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Artifact has no preview.")

    try:
        props = await preview_props(previewer, artifact)
    except PreviewUnavailable as exc:
        raise HTTPException(status_code=http_status.HTTP_502_BAD_GATEWAY, detail=f"Could not fetch artifact: {exc}")
    except PreviewUnreadable as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not read artifact: {exc}"
        )
    except PreviewFailed as exc:
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Preview failed: {exc}")
    except Exception:
        logger.exception("Building the %s preview failed for artifact %s", previewer.name, artifact_id)
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Preview failed.")
    return JSONResponse(props, headers=_CACHE_HEADERS)


def _reject_unsafe_segment(value: str) -> None:
    """Reject any path segment that isn't a single safe name (path-traversal guard)."""
    if not _SAFE_SEGMENT_RE.match(value) or value.startswith("."):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Invalid preview path.")


@router.get("/previews/{key}/{filename}")
async def get_preview_file(
    key: str,
    filename: str,
    claims: TokenClaims = Depends(current_claims),
) -> Response:
    """Serve one file of a built preview (auth-required).

    A preview's props carry ``baseUrl`` = ``/api/previews/{key}``; its viewer fetches
    ``${baseUrl}/${filename}`` from here. The key hashes the source URI and the files
    are session-independent, so the guard is auth + path-traversal safety: both
    segments must be single safe names and the resolved path must stay inside the
    preview cache. Immutable, hence cacheable forever.
    """
    _reject_unsafe_segment(key)
    _reject_unsafe_segment(filename)

    root = preview_root().resolve()
    candidate = (root / key / filename).resolve()
    if not candidate.is_relative_to(root):  # defense in depth beyond the segment check
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Invalid preview path.")
    if not candidate.is_file():
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Preview file not found.")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path=str(candidate), media_type=media_type, headers=_CACHE_HEADERS)
