"""Build previews on demand and cache them on disk.

``GET /api/conversations/{cid}/artifacts/{aid}/preview`` → :func:`preview_props`. The
frontend asks only once the artifact's viewer scrolls into view, so nothing is built
eagerly — not on a live turn, not when a conversation is reopened.

Cache layout under :func:`preview_root`::

    {key}/preview.json   the viewer props the previewer returned
    {key}/<files>        whatever else it wrote, served by GET /api/previews/{key}/{name}

``key`` hashes the previewer's name + version and the artifact's URI. Artifact URIs
name immutable content (DeepLynx items, per-upload staged files), so a built preview
never goes stale and a hit needs no download. A preview is built in a dot-prefixed
scratch directory and renamed into place, so a half-built one is never visible (and
the file route, which rejects dot-prefixed segments, can't reach the scratch
directories). Concurrent requests for the same preview share one build within a web
worker; with ``BACKEND_WORKERS`` > 1 two workers may both build a cold preview, and
the second rename simply loses (``worker.build_preview``) — wasted work, never a
corrupt preview.

Builds run in a subprocess pool (``pool.py``) so a previewer that crashes or runs out
of memory fails one preview instead of taking the web process with it.

Point ``PROMETHEUS_PREVIEW_CACHE_DIR`` at a persistent volume (``docker-compose.yml``
does) so previews survive restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app import uri_resolver
from app.config import settings
from app.services.artifacts import artifact_identity

from . import pool
from .base import Previewer, PreviewUnavailable
from .worker import read_props

logger = logging.getLogger(__name__)

# Where the file route is mounted; a preview's baseUrl is f"{PREVIEW_FILES_PREFIX}/{key}".
PREVIEW_FILES_PREFIX = "/api/previews"


def preview_root() -> Path:
    """Directory holding built previews, one ``{key}/`` directory each.

    Configured via ``preview-cache-dir`` / ``PROMETHEUS_PREVIEW_CACHE_DIR``; defaults
    to ``<tempdir>/prometheus-previews``, which works but is lost with the container.
    """
    configured = settings.preview_cache_dir
    root = Path(configured) if configured else Path(tempfile.gettempdir()) / "prometheus-previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def preview_key(previewer: Previewer, uri: str) -> str:
    return hashlib.sha256(f"{previewer.name}|{previewer.version}|{uri}".encode()).hexdigest()[:32]


async def _resolve_and_build(previewer: Previewer, artifact: dict[str, Any], uri: str, root: Path, key: str) -> None:
    download_dir = Path(tempfile.mkdtemp(prefix="preview-src-"))
    try:
        source = await uri_resolver.resolve_to_local_path(
            uri, cache_dir=download_dir, artifact=artifact, artifact_id=artifact_identity(artifact)
        )
        if source is None or not source.exists():
            raise PreviewUnavailable(f"could not resolve {uri!r}")
        await pool.run_build(previewer, source, artifact, root, key)
    finally:
        # A file:// source resolves to the original path (outside download_dir), so
        # this only removes http(s)/deeplynx downloads.
        shutil.rmtree(download_dir, ignore_errors=True)


# Builds in flight, keyed by cache key. Each is a standalone task, so a browser that
# navigates away mid-build (cancelling its request) doesn't abort work the next viewer
# — or another tab waiting on the same task — will want.
_in_flight: dict[str, asyncio.Task[None]] = {}


async def preview_props(previewer: Previewer, artifact: dict[str, Any]) -> dict[str, Any]:
    """The viewer props for ``artifact``'s preview, building it on first request.

    Raises :class:`PreviewUnavailable` when the source can't be fetched and
    :class:`PreviewFailed` when the build process crashed or timed out; errors from
    the previewer itself (:class:`PreviewUnreadable`, or anything unexpected)
    propagate for the router to map.
    """
    uri = str(artifact.get("uri") or "")
    if not uri:
        raise PreviewUnavailable("artifact has no uri")
    root = preview_root()
    key = preview_key(previewer, uri)

    props = read_props(root / key)
    if props is None:
        task = _in_flight.get(key)
        if task is None:
            task = asyncio.create_task(_resolve_and_build(previewer, artifact, uri, root, key))
            _in_flight[key] = task
            task.add_done_callback(lambda _t: _in_flight.pop(key, None))
        await asyncio.shield(task)
        props = read_props(root / key)
        if props is None:
            raise PreviewUnavailable(f"preview {key} is missing its props after building")
    return {**props, "baseUrl": f"{PREVIEW_FILES_PREFIX}/{key}"}
