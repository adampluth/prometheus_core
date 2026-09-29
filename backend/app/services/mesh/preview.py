"""The mesh previewer: Exodus/STL → ``.vtp`` timesteps + a manifest for the MeshViewer.

Registered in ``app/services/previews/registry.py``; the generic preview service
handles fetching, caching, and serving (see ``app/services/previews``). This module
only knows how to convert a mesh and what props ``MeshViewerElement.tsx`` reads.

IMPORTANT: VTK is imported lazily inside :meth:`MeshPreviewer.build` (which runs in
the preview thread pool), so importing this module — as the previewer registry does
on every artifact shaping — never pulls in ``vtk``.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from app.services.previews.base import PreviewUnreadable

from .exceptions import ConverterError

# vtk.js fetches the timesteps by URL; give the file route a real content type.
mimetypes.add_type("application/xml", ".vtp")

# STL MIME types (a subset of the classifier's mesh MIMEs) that pick the STL reader.
_STL_MIMES = {
    "model/stl",
    "application/stl",
    "application/x-stl",
    "application/sla",
    "application/vnd.ms-pki.stl",
    "model/x.stl-binary",
    "model/x.stl-ascii",
}


def mesh_kind(name: str, uri: str, mime: str) -> str:
    """Pick ``"stl"`` vs ``"exodus"`` from extension (uri, then name), then MIME."""
    for source in (uri, name):
        if source and Path(str(source)).suffix.lower() == ".stl":
            return "stl"
    if mime.split(";", 1)[0].strip().lower() in _STL_MIMES:
        return "stl"
    return "exodus"


def viewer_props(manifest: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """The MeshViewer props for a converted mesh (the title comes from the artifact,
    frontend-side; ``baseUrl`` from the preview service)."""
    variables = manifest.get("variables") or {}
    point_vars = variables.get("point") or []
    cell_vars = variables.get("cell") or []
    initial_variable = point_vars[0] if point_vars else (cell_vars[0] if cell_vars else None)
    return {
        "metadata": metadata,
        "manifest": manifest,
        "initialTimestep": 0,
        "initialVariable": initial_variable,
        "initialColorPreset": "coolToWarm",
        "height": "500px",
        "showControls": True,
        "allowPicking": True,
    }


class MeshPreviewer:
    name = "mesh"
    version = "1"

    def applies(self, artifact: dict[str, Any], render_hint: str) -> bool:
        return render_hint == "mesh" and bool(artifact.get("uri"))

    def build(self, source: Path, artifact: dict[str, Any], out_dir: Path) -> dict[str, Any]:
        name = str(artifact.get("name") or "")
        kind = mesh_kind(name, str(artifact.get("uri") or ""), str(artifact.get("mime_type") or ""))
        try:
            if kind == "stl":
                from .stl import convert_stl_sync

                convert_stl_sync(source, out_dir)
            else:
                from .converter import convert_exodus
                from .models import ConvertRequest

                convert_exodus(ConvertRequest(input_path=source, output_dir=out_dir, timestep_mode="all"))
        except ConverterError as exc:
            raise PreviewUnreadable(str(exc)) from exc
        manifest = json.loads((out_dir / "manifest.json").read_text())
        metadata = json.loads((out_dir / "meta.json").read_text())
        if not metadata.get("num_nodes"):
            # VTK's Exodus reader "succeeds" on a file it can't parse, yielding an empty
            # mesh — which would otherwise be cached as this source's preview forever.
            raise PreviewUnreadable(f"{name or 'mesh'} contains no geometry")
        return viewer_props(manifest, metadata)
