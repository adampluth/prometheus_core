"""Tests for the mesh previewer (Exodus/STL → VTP for the MeshViewer).

Caching, dedupe, and serving are generic and live in ``test_previews.py``; this file
covers only what the mesh previewer knows: when it applies, which reader a source
gets, and the props it hands the MeshViewer. The converter round-trips are guarded by
``pytest.importorskip("vtk")`` so they run only where VTK is installed (the
container), never breaking the offline suite.
"""

import json

import pytest

from app.services.mesh.preview import MeshPreviewer, mesh_kind, viewer_props
from app.services.previews import PreviewUnreadable
from app.services.previews.registry import previewer_for


@pytest.mark.parametrize(
    "name,uri,mime,expected",
    [
        ("mesh.stl", "", "", "stl"),
        ("", "file:///data/model.STL", "", "stl"),  # extension is case-insensitive
        ("", "", "model/stl", "stl"),
        ("", "", "application/vnd.ms-pki.stl; charset=binary", "stl"),
        ("mesh.exo", "", "", "exodus"),
        ("mesh.e", "", "", "exodus"),
        ("data", "", "application/x-exodus", "exodus"),
        ("", "", "", "exodus"),  # default
        ("mesh.stl", "", "application/x-exodus", "stl"),  # extension wins over a mismatched mime
    ],
)
def test_mesh_kind(name, uri, mime, expected):
    assert mesh_kind(name, uri, mime) == expected


def _manifest(point=None, cell=None):
    return {
        "version": "1.0",
        "num_timesteps": 1,
        "files": [{"index": 0, "timestep_index": 0, "time_value": 0.0, "filename": "surface_t0000.vtp"}],
        "variables": {"point": point or [], "cell": cell or []},
        "array_ranges": {},
        "bounds": [0, 1, 0, 1, 0, 1],
    }


def test_viewer_props_shape():
    metadata = {"num_timesteps": 2, "time_values": [0.0, 1.0]}
    props = viewer_props(_manifest(point=["temperature"], cell=["block_id"]), metadata)

    assert props["metadata"] is metadata
    assert props["manifest"]["files"][0]["filename"] == "surface_t0000.vtp"
    assert props["initialColorPreset"] == "coolToWarm"
    assert props["showControls"] is True
    assert props["allowPicking"] is True
    assert "baseUrl" not in props and "title" not in props  # added by the service / frontend


def test_viewer_props_initial_variable_prefers_point_then_cell_then_none():
    m = {"num_timesteps": 1, "time_values": [0.0]}
    assert viewer_props(_manifest(point=["p1"], cell=["c1"]), m)["initialVariable"] == "p1"
    assert viewer_props(_manifest(cell=["c1"]), m)["initialVariable"] == "c1"
    assert viewer_props(_manifest(), m)["initialVariable"] is None


def test_mesh_previewer_applies_to_meshes_with_a_source():
    p = MeshPreviewer()
    assert p.applies({"uri": "file:///m.exo"}, "mesh")
    assert not p.applies({}, "mesh")  # nothing to build from
    assert not p.applies({"uri": "file:///r.pdf"}, "pdf")
    assert isinstance(previewer_for({"uri": "file:///m.exo"}, "mesh"), MeshPreviewer)
    assert previewer_for({"uri": "file:///r.pdf"}, "pdf") is None


def test_mesh_previewer_wraps_converter_errors(tmp_path):
    pytest.importorskip("vtk")
    bogus = tmp_path / "bogus.exo"
    bogus.write_bytes(b"not an exodus file")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(PreviewUnreadable):
        MeshPreviewer().build(bogus, {"name": "bogus.exo", "uri": "file:///bogus.exo"}, out)


def _write_sphere_stl(vtk, path):
    sphere = vtk.vtkSphereSource()
    sphere.Update()
    writer = vtk.vtkSTLWriter()
    writer.SetFileName(str(path))
    writer.SetInputConnection(sphere.GetOutputPort())
    writer.Write()


def test_stl_conversion_roundtrip(tmp_path):
    vtk = pytest.importorskip("vtk")
    from app.services.mesh.stl import convert_stl_sync

    stl_path = tmp_path / "sphere.stl"
    _write_sphere_stl(vtk, stl_path)

    out = tmp_path / "out"
    metadata = convert_stl_sync(stl_path, out)

    assert (out / "surface_t0000.vtp").is_file()
    assert (out / "manifest.json").is_file()
    assert (out / "meta.json").is_file()
    assert metadata["num_timesteps"] == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["files"][0]["filename"] == "surface_t0000.vtp"
    assert manifest["variables"] == {"point": [], "cell": []}


def test_mesh_previewer_builds_stl(tmp_path):
    vtk = pytest.importorskip("vtk")
    stl_path = tmp_path / "sphere.stl"
    _write_sphere_stl(vtk, stl_path)
    out = tmp_path / "out"
    out.mkdir()

    props = MeshPreviewer().build(stl_path, {"name": "sphere.stl", "mime_type": "model/stl"}, out)

    assert (out / "surface_t0000.vtp").is_file()
    assert props["metadata"]["num_elements"] > 0
    assert props["manifest"]["files"][0]["filename"] == "surface_t0000.vtp"
