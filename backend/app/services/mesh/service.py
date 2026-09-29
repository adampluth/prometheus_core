"""App-facing mesh conversion: turn a mesh artifact into MeshViewer props, on demand.

The frontend asks for a mesh only when its viewer scrolls into view
(``GET /api/conversations/{cid}/artifacts/{aid}/mesh`` → :func:`mesh_viewer_props`).
That resolves the raw Exodus/STL bytes (reusing
:func:`app.uri_resolver.resolve_to_local_path`), converts them to ``.vtp`` timesteps +
a manifest off the event loop (thread pool), and returns the exact prop shape
``frontend/src/viewers/mesh/MeshViewerElement.tsx`` reads. The ``.vtp`` files are
served by ``GET /api/artifacts/mesh/{dataset_id}/{filename}`` (see
``routers/artifacts.py``), so ``baseUrl`` points there.

Nothing converts eagerly — not a live turn, not reopening a conversation — so a chat
with many meshes renders its text immediately and pays only for the meshes the user
actually looks at.

Caching, cheapest first:

1. **Source index** — ``{cache_root}/.index/{key}.json`` maps an artifact's source
   (URI + mesh kind) to its ``dataset_id``. A hit reads the cached manifest straight
   off disk: no download, no hashing, no VTK. Artifact URIs name immutable content
   (DeepLynx items, per-upload staged files), so the mapping never goes stale.
2. **Content-hash cache** — ``{cache_root}/{dataset_id}/`` (see ``cache.py``). A new
   URI for bytes already converted still has to download + hash, but skips VTK.
3. **In-flight dedupe** — concurrent requests for the same source share one
   conversion task (several tabs, or a mesh repeated within a conversation).

Point ``PROMETHEUS_MESH_CACHE_DIR`` at a persistent volume (``docker-compose.yml``
does) so all of this survives restarts.

IMPORTANT: the VTK-dependent converter modules are imported lazily inside
:func:`_convert_sync` (which runs in the thread pool), so importing this module — as
``routers/artifacts.py`` does — never pulls in ``vtk``. The offline backend test
suite has no VTK installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from app import uri_resolver
from app.config import settings
from app.services.artifacts import _normalize_mime, artifact_identity

from .cache import get_cache_path, is_cached

logger = logging.getLogger(__name__)

# URL prefix the VTP-serving route is mounted at; baseUrl = f"{MESH_URL_PREFIX}/{id}".
MESH_URL_PREFIX = "/api/artifacts/mesh"

# STL MIME types (a subset of _MESH_MIMETYPES) that pick the STL reader over Exodus.
_STL_MIMES = {
    "model/stl",
    "application/stl",
    "application/x-stl",
    "application/sla",
    "application/vnd.ms-pki.stl",
    "model/x.stl-binary",
    "model/x.stl-ascii",
}

# Source index directory under the cache root. The leading dot keeps it out of the
# VTP route's reach (``routers/artifacts.py`` rejects dot-prefixed segments).
_INDEX_DIR = ".index"

# Bumped if conversion parameters change, so old index entries stop matching.
_INDEX_VERSION = "v1"


class MeshUnavailable(Exception):
    """The artifact's source mesh could not be resolved to local bytes."""


# Dedicated thread pool for CPU-bound VTK work — 2 workers to bound memory on large
# meshes (mirrors the old components/mesh_view.py::_vtk_executor).
_vtk_executor: Optional[ThreadPoolExecutor] = None


def _executor() -> ThreadPoolExecutor:
    global _vtk_executor
    if _vtk_executor is None:
        _vtk_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mesh-vtk-")
    return _vtk_executor


def mesh_cache_root() -> Path:
    """Directory holding converted ``{dataset_id}/`` VTP sets.

    Configured via ``mesh-cache-dir`` / ``PROMETHEUS_MESH_CACHE_DIR``; defaults to
    ``<tempdir>/prometheus-mesh``, which works but is lost with the container —
    deployments should configure a persistent volume.
    """
    configured = settings.mesh_cache_dir
    root = Path(configured) if configured else Path(tempfile.gettempdir()) / "prometheus-mesh"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _mesh_kind(name: str, uri: str, mime: str) -> str:
    """Pick ``"stl"`` vs ``"exodus"`` from extension (uri, then name), then MIME."""
    for source in (uri, name):
        if source and Path(str(source)).suffix.lower() == ".stl":
            return "stl"
    if mime in _STL_MIMES:
        return "stl"
    return "exodus"


def _build_json_body(dataset_id: str, manifest: dict, metadata: dict, title: str) -> dict[str, Any]:
    """Assemble the MeshViewer prop shape (ported from old MeshDataset.to_props()).

    Only ``baseUrl`` differs from the old props: it points at the backend VTP route
    instead of Chainlit's ``/public/mesh-data/{id}``.
    """
    variables = manifest.get("variables") or {}
    point_vars = variables.get("point") or []
    cell_vars = variables.get("cell") or []
    initial_variable = point_vars[0] if point_vars else (cell_vars[0] if cell_vars else None)
    return {
        "datasetId": dataset_id,
        "baseUrl": f"{MESH_URL_PREFIX}/{dataset_id}",
        "title": title,
        "metadata": metadata,
        "manifest": manifest,
        "initialTimestep": 0,
        "initialVariable": initial_variable,
        "initialColorPreset": "coolToWarm",
        "height": "500px",
        "showControls": True,
        "allowPicking": True,
    }


def _convert_sync(local_path: Path, cache_root: Path, name: str, uri: str, mime: str) -> str:
    """Convert a resolved mesh file to a cached VTP set; return its ``dataset_id``.

    Runs in the thread pool. VTK is imported lazily here so the module import graph
    stays vtk-free. Both converters write ``manifest.json`` + ``meta.json`` into the
    dataset directory, which is all :func:`_props_from_cache` needs afterwards.
    """
    if _mesh_kind(name, uri, mime) == "stl":
        from .cache import compute_dataset_id
        from .stl import convert_stl_sync

        dataset_id = compute_dataset_id(local_path, {"type": "stl"})
        if not is_cached(cache_root, dataset_id):
            convert_stl_sync(local_path, get_cache_path(cache_root, dataset_id))
        return dataset_id

    from .converter import convert_exodus
    from .models import ConvertRequest

    request = ConvertRequest(input_path=local_path, output_dir=cache_root, timestep_mode="all")
    return convert_exodus(request, cache_root=cache_root).dataset_id


def _index_path(cache_root: Path, uri: str, kind: str) -> Path:
    key = hashlib.sha256(f"{_INDEX_VERSION}|{kind}|{uri}".encode()).hexdigest()[:32]
    return cache_root / _INDEX_DIR / f"{key}.json"


def _read_index(path: Path) -> Optional[str]:
    try:
        dataset_id = json.loads(path.read_text()).get("dataset_id")
    except (OSError, ValueError, AttributeError):
        return None
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def _write_index(path: Path, dataset_id: str, uri: str) -> None:
    """Atomically record ``uri → dataset_id`` (a torn write must never be read)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"dataset_id": dataset_id, "uri": uri}))
    os.replace(tmp, path)


def _props_from_cache(cache_root: Path, dataset_id: str, title: str) -> Optional[dict[str, Any]]:
    """MeshViewer props for an already-converted dataset, or ``None`` if it's gone."""
    output_dir = get_cache_path(cache_root, dataset_id)
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text())
        metadata = json.loads((output_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    return _build_json_body(dataset_id, manifest, metadata, title)


async def _resolve_and_convert(artifact: dict[str, Any], uri: str, cache_root: Path, index: Path) -> str:
    """Download the source, convert it, record it in the index; return ``dataset_id``."""
    ident = artifact_identity(artifact)
    name = str(artifact.get("name") or "")
    mime = _normalize_mime(artifact.get("mime_type"))

    download_dir = Path(tempfile.mkdtemp(prefix="mesh-src-"))
    try:
        local_path = await uri_resolver.resolve_to_local_path(
            uri, cache_dir=download_dir, artifact=artifact, artifact_id=ident
        )
        if local_path is None or not local_path.exists():
            raise MeshUnavailable(f"could not resolve {uri!r}")
        loop = asyncio.get_running_loop()
        dataset_id = await loop.run_in_executor(_executor(), _convert_sync, local_path, cache_root, name, uri, mime)
    finally:
        # For file:// sources resolve_to_local_path returns the original path (outside
        # download_dir), so this only cleans up http(s)/deeplynx downloads.
        shutil.rmtree(download_dir, ignore_errors=True)

    _write_index(index, dataset_id, uri)
    return dataset_id


# Conversions in flight, keyed by index path. Each is a standalone task, so a browser
# that navigates away mid-conversion (cancelling its request) doesn't abort work the
# next viewer — or another tab waiting on the same task — will want.
_in_flight: dict[Path, asyncio.Task[str]] = {}


async def mesh_viewer_props(artifact: dict[str, Any]) -> dict[str, Any]:
    """MeshViewer props for a mesh artifact, converting it on first request.

    Raises :class:`MeshUnavailable` when the source can't be resolved, and lets
    converter errors propagate; the router maps both to an error response the viewer
    shows alongside a download link.
    """
    uri = str(artifact.get("uri") or "")
    if not uri:
        raise MeshUnavailable("artifact has no uri")
    name = str(artifact.get("name") or "")
    title = name or "Mesh Viewer"
    kind = _mesh_kind(name, uri, _normalize_mime(artifact.get("mime_type")))
    cache_root = mesh_cache_root()
    index = _index_path(cache_root, uri, kind)

    dataset_id = _read_index(index)
    if dataset_id is not None:
        props = _props_from_cache(cache_root, dataset_id, title)
        if props is not None:
            return props
        # Index outlived its dataset (cache pruned by hand) — fall through and rebuild.

    task = _in_flight.get(index)
    if task is None:
        task = asyncio.create_task(_resolve_and_convert(artifact, uri, cache_root, index))
        _in_flight[index] = task
        task.add_done_callback(lambda _t: _in_flight.pop(index, None))
    dataset_id = await asyncio.shield(task)

    props = _props_from_cache(cache_root, dataset_id, title)
    if props is None:
        raise MeshUnavailable(f"converted dataset {dataset_id} is missing its manifest")
    return props
