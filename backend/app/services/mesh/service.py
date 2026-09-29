"""App-facing mesh enrichment: populate mesh artifacts' ``json_body`` with a VTP manifest.

For every artifact the classifier routes to the ``"mesh"`` render hint, this resolves
the raw Exodus/STL bytes (reusing :func:`app.uri_resolver.resolve_to_local_path`),
converts them to ``.vtp`` timesteps + a manifest off the event loop (thread pool,
content-hash cached), and sets ``json_body`` to the exact prop shape
``frontend/src/viewers/mesh/MeshViewerElement.tsx`` reads. The ``.vtp`` files are served
by ``GET /api/artifacts/mesh/{dataset_id}/{filename}`` (see ``routers/artifacts.py``),
so ``baseUrl`` points there.

On any failure the artifact's ``json_body`` is left unset (``None``) so the frontend
adapter cleanly degrades to a file download — restoring rendering must never break a turn.

IMPORTANT: the VTK-dependent converter modules are imported lazily inside
:func:`_convert_sync` (which runs in the thread pool), so importing this module — as
``routers/artifacts.py`` and ``services/chat.py`` both do — never pulls in ``vtk``. The
offline backend test suite has no VTK installed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from app import uri_resolver
from app.config import settings
from app.services.artifacts import _normalize_mime, artifact_identity, classify_render_hint

if TYPE_CHECKING:  # avoid importing the prometheus_api contract at runtime
    from app.a2a_client import ConversationResponse

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
    ``<tempdir>/prometheus-mesh``. Persistent for the service lifetime (NOT the
    per-request scratch dir the content route uses), since the browser fetches the
    VTP files after the turn completes.
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


def _convert_sync(local_path: Path, cache_root: Path, name: str, uri: str, mime: str) -> tuple[str, dict, dict]:
    """Convert a resolved mesh file to a cached VTP set. Runs in the thread pool.

    Returns ``(dataset_id, manifest, metadata)``. VTK is imported lazily here so the
    module import graph stays vtk-free.
    """
    import json

    if _mesh_kind(name, uri, mime) == "stl":
        from .cache import compute_dataset_id, get_cache_path, is_cached
        from .stl import convert_stl_sync

        dataset_id = compute_dataset_id(local_path, {"type": "stl"})
        output_dir = get_cache_path(cache_root, dataset_id)
        if not is_cached(cache_root, dataset_id):
            convert_stl_sync(local_path, output_dir)
        manifest = json.loads((output_dir / "manifest.json").read_text())
        metadata = json.loads((output_dir / "meta.json").read_text())
        return dataset_id, manifest, metadata

    from .converter import convert_exodus
    from .models import ConvertRequest

    request = ConvertRequest(input_path=local_path, output_dir=cache_root, timestep_mode="all")
    result = convert_exodus(request, cache_root=cache_root)
    manifest = json.loads((result.output_dir / "manifest.json").read_text())
    return result.dataset_id, manifest, result.metadata


async def _enrich_one(artifact: dict[str, Any], uri: str, cache_root: Path) -> Optional[dict[str, Any]]:
    """Resolve + convert one mesh artifact into its ``json_body``, or ``None`` on failure."""
    ident = artifact_identity(artifact)
    name = str(artifact.get("name") or "")
    mime = _normalize_mime(artifact.get("mime_type"))

    download_dir = Path(tempfile.mkdtemp(prefix="mesh-src-"))
    try:
        local_path = await uri_resolver.resolve_to_local_path(
            uri, cache_dir=download_dir, artifact=artifact, artifact_id=ident
        )
        if local_path is None or not local_path.exists():
            logger.warning("mesh: could not resolve %r for artifact %s", uri, ident)
            return None
        loop = asyncio.get_running_loop()
        dataset_id, manifest, metadata = await loop.run_in_executor(
            _executor(), _convert_sync, local_path, cache_root, name, uri, mime
        )
    finally:
        # For file:// sources resolve_to_local_path returns the original path (outside
        # download_dir), so this only cleans up http(s)/deeplynx downloads.
        shutil.rmtree(download_dir, ignore_errors=True)

    return _build_json_body(dataset_id, manifest, metadata, name or "Mesh Viewer")


async def enrich_mesh_artifacts(response: "ConversationResponse") -> None:
    """Populate ``json_body`` for every mesh artifact on ``response``, in place.

    Enriches across the union of ``new_artifacts`` (shaped for the wire) and
    ``artifacts`` (the persisted catalog), deduped by identity so each dataset is
    converted once.
    """
    await enrich_mesh_artifact_list(
        list(getattr(response, "new_artifacts", None) or []) + list(getattr(response, "artifacts", None) or [])
    )


async def enrich_mesh_artifact_list(artifacts: list[Any]) -> None:
    """Populate ``json_body`` for every mesh artifact dict in *artifacts*, in place.

    Used for live turns (via :func:`enrich_mesh_artifacts`) and for reopening a
    past conversation, whose Redis catalog never carries ``json_body``. Artifacts
    sharing an identity are converted once, and the content-hash cache makes a
    repeat (e.g. reopening the same conversation) free. Failures are swallowed —
    the artifact keeps ``json_body: None``.
    """
    all_artifacts: list[dict[str, Any]] = [a for a in artifacts if isinstance(a, dict)]
    if not all_artifacts:
        return

    # First occurrence (by identity) of each mesh artifact still needing a body.
    to_convert: dict[str, dict[str, Any]] = {}
    for artifact in all_artifacts:
        ident = artifact_identity(artifact)
        if ident in to_convert:
            continue
        if artifact.get("json_body") is not None:
            continue
        if classify_render_hint(artifact) != "mesh":
            continue
        if not (artifact.get("uri") or ""):
            continue
        to_convert[ident] = artifact

    if not to_convert:
        return

    cache_root = mesh_cache_root()
    bodies: dict[str, dict[str, Any]] = {}
    for ident, artifact in to_convert.items():
        try:
            body = await _enrich_one(artifact, str(artifact["uri"]), cache_root)
        except Exception:
            logger.exception("mesh enrichment failed for artifact %s", ident)
            continue
        if body is not None:
            bodies[ident] = body

    # Apply each computed body to every artifact object sharing that identity.
    for artifact in all_artifacts:
        if artifact.get("json_body") is not None:
            continue
        body = bodies.get(artifact_identity(artifact))
        if body is not None:
            artifact["json_body"] = body
