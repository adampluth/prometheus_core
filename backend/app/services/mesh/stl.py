"""STL → VTP conversion — the single-timestep / no-variable case of the mesh pipeline.

Ported from the old ``frontend/components/stl_integration.py``, dropping the Chainlit
element construction, the ``utils.settings`` dependency, the ``MeshDataset`` wrapper
and the async wrappers (the mesh previewer, :mod:`app.services.mesh.preview`, calls this; the generic
preview service owns the async + cache orchestration). An STL is converted to exactly the same VTP +
``manifest.json`` + ``meta.json`` output the Exodus path produces, so the shared React
MeshViewer renders it identically (it degrades to "one timestep, no variables").

``import vtk`` at load time — import this lazily from a conversion code path only.
"""

from __future__ import annotations

import json
from pathlib import Path

import vtk

from .exceptions import InvalidExodusFileError
from .vtk_pipeline import clean_polydata, compute_normals, write_vtp


class InvalidSTLFileError(InvalidExodusFileError):
    """Raised when a file cannot be read as STL."""


def _read_stl_polydata(file_path: Path) -> "vtk.vtkPolyData":
    """Read an STL (ASCII or binary) into polydata, validating it has triangles."""
    reader = vtk.vtkSTLReader()
    reader.SetFileName(str(file_path))
    reader.Update()
    polydata = reader.GetOutput()
    if polydata is None or polydata.GetNumberOfCells() == 0:
        raise InvalidSTLFileError(f"No triangles read from STL file: {file_path}")
    return polydata


def _bounds(polydata: "vtk.vtkPolyData") -> list[float]:
    bounds = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    polydata.GetBounds(bounds)
    return list(bounds)


def build_manifest_and_meta(polydata: "vtk.vtkPolyData") -> tuple[dict, dict]:
    """Build the single-timestep manifest + meta dicts for an STL surface."""
    bounds = _bounds(polydata)
    manifest = {
        "version": "1.0",
        "num_timesteps": 1,
        "files": [{"index": 0, "timestep_index": 0, "time_value": 0.0, "filename": "surface_t0000.vtp"}],
        "variables": {"point": [], "cell": []},
        "array_ranges": {},
        "bounds": bounds,
    }
    metadata = {
        "num_timesteps": 1,
        "time_values": [0.0],
        "nodal_variables": [],
        "element_variables": [],
        "global_variables": [],
        "element_blocks": [],
        "bounds": bounds,
        "num_nodes": polydata.GetNumberOfPoints(),
        "num_elements": polydata.GetNumberOfCells(),
    }
    return manifest, metadata


def convert_stl_sync(file_path: Path, output_dir: Path) -> dict:
    """Convert an STL file to a single VTP plus ``manifest.json`` / ``meta.json``.

    Mirrors the output contract of :func:`vtk_pipeline.run_conversion` so the result
    renders identically in the shared MeshViewer. Returns the metadata dict.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    polydata = _read_stl_polydata(file_path)
    # STL is already a triangle surface; clean merges duplicate vertices and
    # compute_normals gives correct shading (STL normals are unreliable).
    polydata = clean_polydata(polydata)
    polydata = compute_normals(polydata, style="flat")
    write_vtp(polydata, output_dir / "surface_t0000.vtp", compress=True)

    manifest, metadata = build_manifest_and_meta(polydata)
    with open(output_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    with open(output_dir / "meta.json", "w") as f:
        json.dump(metadata, f, indent=2)
    return metadata
