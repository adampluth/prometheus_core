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
directories). Concurrent requests for the same preview share one build.

Point ``PROMETHEUS_PREVIEW_CACHE_DIR`` at a persistent volume (``docker-compose.yml``
does) so previews survive restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from app import uri_resolver
from app.config import settings
from app.services.artifacts import artifact_identity

from .base import Previewer, PreviewUnavailable

logger = logging.getLogger(__name__)

# Where the file route is mounted; a preview's baseUrl is f"{PREVIEW_FILES_PREFIX}/{key}".
PREVIEW_FILES_PREFIX = "/api/previews"

_PROPS_FILE = "preview.json"

# Builds are CPU- and memory-heavy (VTK on a large mesh), so keep the pool small.
_build_executor: Optional[ThreadPoolExecutor] = None


def _executor() -> ThreadPoolExecutor:
    global _build_executor
    if _build_executor is None:
        _build_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview-")
    return _build_executor


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


def _read_props(preview_dir: Path) -> Optional[dict[str, Any]]:
    try:
        props = json.loads((preview_dir / _PROPS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return props if isinstance(props, dict) else None


def _build_sync(previewer: Previewer, source: Path, artifact: dict[str, Any], root: Path, key: str) -> None:
    """Build into a scratch directory, then rename it into place (worker thread)."""
    scratch = root / f".build-{key}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir()
    try:
        props = previewer.build(source, artifact, scratch)
        (scratch / _PROPS_FILE).write_text(json.dumps(props))
        final = root / key
        if final.exists():
            # Left over from a build whose props file is gone (pruned by hand).
            shutil.rmtree(final, ignore_errors=True)
        os.rename(scratch, final)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _resolve_and_build(previewer: Previewer, artifact: dict[str, Any], uri: str, root: Path, key: str) -> None:
    download_dir = Path(tempfile.mkdtemp(prefix="preview-src-"))
    try:
        source = await uri_resolver.resolve_to_local_path(
            uri, cache_dir=download_dir, artifact=artifact, artifact_id=artifact_identity(artifact)
        )
        if source is None or not source.exists():
            raise PreviewUnavailable(f"could not resolve {uri!r}")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor(), _build_sync, previewer, source, artifact, root, key)
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

    Raises :class:`PreviewUnavailable` when the source can't be fetched; errors from
    the previewer itself (:class:`PreviewUnreadable`, or anything unexpected)
    propagate for the router to map.
    """
    uri = str(artifact.get("uri") or "")
    if not uri:
        raise PreviewUnavailable("artifact has no uri")
    root = preview_root()
    key = preview_key(previewer, uri)

    props = _read_props(root / key)
    if props is None:
        task = _in_flight.get(key)
        if task is None:
            task = asyncio.create_task(_resolve_and_build(previewer, artifact, uri, root, key))
            _in_flight[key] = task
            task.add_done_callback(lambda _t: _in_flight.pop(key, None))
        await asyncio.shield(task)
        props = _read_props(root / key)
        if props is None:
            raise PreviewUnavailable(f"preview {key} is missing its props after building")
    return {**props, "baseUrl": f"{PREVIEW_FILES_PREFIX}/{key}"}
