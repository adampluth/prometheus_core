"""Tests for the mesh (Exodus/STL → VTP) restoration.

Most tests are offline (no VTK): the json_body/prop shape, the source-kind picker,
on-demand conversion (``mesh_viewer_props``: source index, in-flight dedupe, failure
handling — with the VTK conversion + URI resolver stubbed), and the VTP
route (auth / 404 / path-traversal, serving a pre-seeded cache file). The converter
round-trip is guarded by ``pytest.importorskip("vtk")`` so it runs only where VTK is
installed (the container), never breaking the offline suite.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import uri_resolver
from app.services.mesh import service


# --------------------------------------------------------------------------- #
# _mesh_kind — STL vs Exodus dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,uri,mime,expected",
    [
        ("mesh.stl", "", "", "stl"),
        ("", "file:///data/model.STL", "", "stl"),  # extension is case-insensitive
        ("", "", "model/stl", "stl"),
        ("", "", "application/vnd.ms-pki.stl", "stl"),
        ("mesh.exo", "", "", "exodus"),
        ("mesh.e", "", "", "exodus"),
        ("data", "", "application/x-exodus", "exodus"),
        ("", "", "", "exodus"),  # default
        # extension wins over a mismatched mime
        ("mesh.stl", "", "application/x-exodus", "stl"),
    ],
)
def test_mesh_kind(name, uri, mime, expected):
    assert service._mesh_kind(name, uri, mime) == expected


# --------------------------------------------------------------------------- #
# _build_json_body — the exact prop shape MeshViewerElement.tsx reads
# --------------------------------------------------------------------------- #


def _manifest(point=None, cell=None):
    return {
        "version": "1.0",
        "num_timesteps": 1,
        "files": [{"index": 0, "timestep_index": 0, "time_value": 0.0, "filename": "surface_t0000.vtp"}],
        "variables": {"point": point or [], "cell": cell or []},
        "array_ranges": {},
        "bounds": [0, 1, 0, 1, 0, 1],
    }


def test_build_json_body_shape_and_baseurl():
    metadata = {"num_timesteps": 2, "time_values": [0.0, 1.0]}
    body = service._build_json_body("ds123", _manifest(point=["temperature"], cell=["block_id"]), metadata, "My Mesh")

    assert body["datasetId"] == "ds123"
    assert body["baseUrl"] == "/api/artifacts/mesh/ds123"
    assert body["baseUrl"] == f"{service.MESH_URL_PREFIX}/ds123"
    assert body["title"] == "My Mesh"
    assert body["metadata"] is metadata
    assert body["manifest"]["files"][0]["filename"] == "surface_t0000.vtp"
    assert body["initialColorPreset"] == "coolToWarm"
    assert body["showControls"] is True
    assert body["allowPicking"] is True


def test_build_json_body_initial_variable_prefers_point_then_cell_then_none():
    m = {"num_timesteps": 1, "time_values": [0.0]}
    assert service._build_json_body("d", _manifest(point=["p1"], cell=["c1"]), m, "t")["initialVariable"] == "p1"
    assert service._build_json_body("d", _manifest(cell=["c1"]), m, "t")["initialVariable"] == "c1"
    assert service._build_json_body("d", _manifest(), m, "t")["initialVariable"] is None


# --------------------------------------------------------------------------- #
# enrich_mesh_artifacts — the turn-time hook (VTK + resolver stubbed)
# --------------------------------------------------------------------------- #


def _stub_resolver(monkeypatch, tmp_path):
    """Make resolve_to_local_path return a real (existing) local file; count calls."""
    src = tmp_path / "mesh.exo"
    src.write_bytes(b"fake-exodus-bytes")
    calls = []

    async def _resolve(uri, *, cache_dir, artifact=None, artifact_id=None):
        calls.append(uri)
        return src

    monkeypatch.setattr(uri_resolver, "resolve_to_local_path", _resolve)
    return calls


def _stub_converter(monkeypatch, dataset_id="dsAAA", *, delay=0.0):
    """Replace the VTK conversion with one that writes a manifest + meta; count calls."""
    calls = []

    def _fake_convert(local_path, cache_root, name, uri, mime):
        calls.append((name, mime))
        time.sleep(delay)
        out = cache_root / dataset_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.json").write_text(json.dumps(_manifest(point=["temperature"])))
        (out / "meta.json").write_text(json.dumps({"num_timesteps": 1, "time_values": [0.0]}))
        return dataset_id

    monkeypatch.setattr(service, "_convert_sync", _fake_convert)
    return calls


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    from app.config import settings

    root = tmp_path / "cache"
    monkeypatch.setattr(settings, "mesh_cache_dir", str(root), raising=False)
    return root


_MESH = {"artifact_id": "m1", "name": "mesh.exo", "mime_type": "application/x-exodus", "uri": "file:///x/mesh.exo"}


async def test_viewer_props_converts_on_first_request(monkeypatch, tmp_path, cache_dir):
    _stub_resolver(monkeypatch, tmp_path)
    converts = _stub_converter(monkeypatch)

    props = await service.mesh_viewer_props(dict(_MESH))

    assert len(converts) == 1
    assert props["datasetId"] == "dsAAA"
    assert props["baseUrl"] == "/api/artifacts/mesh/dsAAA"
    assert props["initialVariable"] == "temperature"
    assert props["title"] == "mesh.exo"


async def test_viewer_props_warm_hit_skips_download_and_conversion(monkeypatch, tmp_path, cache_dir):
    downloads = _stub_resolver(monkeypatch, tmp_path)
    converts = _stub_converter(monkeypatch)
    await service.mesh_viewer_props(dict(_MESH))

    props = await service.mesh_viewer_props({**_MESH, "name": "renamed.exo"})

    assert len(downloads) == 1 and len(converts) == 1  # served from the source index
    assert props["datasetId"] == "dsAAA"
    assert props["title"] == "renamed.exo"  # title comes from the artifact, not the cache


async def test_viewer_props_rebuilds_when_indexed_dataset_is_gone(monkeypatch, tmp_path, cache_dir):
    import shutil

    _stub_resolver(monkeypatch, tmp_path)
    converts = _stub_converter(monkeypatch)
    await service.mesh_viewer_props(dict(_MESH))
    shutil.rmtree(cache_dir / "dsAAA")

    props = await service.mesh_viewer_props(dict(_MESH))

    assert len(converts) == 2 and props["datasetId"] == "dsAAA"


async def test_viewer_props_dedupes_concurrent_requests(monkeypatch, tmp_path, cache_dir):
    downloads = _stub_resolver(monkeypatch, tmp_path)
    converts = _stub_converter(monkeypatch, delay=0.1)

    results = await asyncio.gather(*(service.mesh_viewer_props(dict(_MESH)) for _ in range(3)))

    assert len(downloads) == 1 and len(converts) == 1
    assert {r["datasetId"] for r in results} == {"dsAAA"}


async def test_viewer_props_conversion_failure_propagates_and_is_retried(monkeypatch, tmp_path, cache_dir):
    _stub_resolver(monkeypatch, tmp_path)

    def _fail(*a):
        raise RuntimeError("vtk blew up")

    monkeypatch.setattr(service, "_convert_sync", _fail)
    with pytest.raises(RuntimeError):
        await service.mesh_viewer_props(dict(_MESH))
    assert not (cache_dir / ".index").exists()  # nothing recorded for a failed source

    converts = _stub_converter(monkeypatch)
    assert (await service.mesh_viewer_props(dict(_MESH)))["datasetId"] == "dsAAA"
    assert len(converts) == 1


async def test_viewer_props_unresolvable_source(monkeypatch, cache_dir):
    async def _none(uri, *, cache_dir, artifact=None, artifact_id=None):
        return None

    monkeypatch.setattr(uri_resolver, "resolve_to_local_path", _none)
    with pytest.raises(service.MeshUnavailable):
        await service.mesh_viewer_props(dict(_MESH))


async def test_viewer_props_requires_uri(monkeypatch, cache_dir):
    def _boom(*a):
        raise AssertionError("conversion must not run without a uri")

    monkeypatch.setattr(service, "_convert_sync", _boom)
    with pytest.raises(service.MeshUnavailable):
        await service.mesh_viewer_props({"artifact_id": "m3", "name": "mesh.exo"})


# --------------------------------------------------------------------------- #
# GET /api/artifacts/mesh/{dataset_id}/{filename}
# --------------------------------------------------------------------------- #


def _authed_client(monkeypatch, tmp_path) -> TestClient:
    """TestClient with a valid session cookie and the mesh cache root at tmp_path."""
    from app import security
    from app.config import settings
    from app.main import app
    from app.sessions import SessionStore

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "mesh_cache_dir", str(tmp_path), raising=False)

    app.state.session_store = SessionStore()
    token = security.issue_token(security.User(identifier="alice"), sid="sid-mesh")
    client = TestClient(app)
    client.cookies.set(settings.cookie_name, token)
    return client


def test_mesh_route_requires_auth(tmp_path, monkeypatch):
    from app.config import settings
    from app.main import app
    from app.sessions import SessionStore

    monkeypatch.setattr(settings, "mesh_cache_dir", str(tmp_path), raising=False)
    app.state.session_store = SessionStore()
    client = TestClient(app)
    assert client.get("/api/artifacts/mesh/ds1/surface_t0000.vtp").status_code == 401


def test_mesh_route_serves_cached_vtp(monkeypatch, tmp_path):
    ds = tmp_path / "ds1"
    ds.mkdir()
    (ds / "surface_t0000.vtp").write_text("<VTKFile type='PolyData'></VTKFile>")

    client = _authed_client(monkeypatch, tmp_path)
    resp = client.get("/api/artifacts/mesh/ds1/surface_t0000.vtp")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "VTKFile" in resp.text


def test_mesh_route_404_when_missing(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    assert client.get("/api/artifacts/mesh/ds1/surface_t0000.vtp").status_code == 404


def test_mesh_route_rejects_directory_as_file(monkeypatch, tmp_path):
    (tmp_path / "ds1").mkdir()  # exists but is a dir, not a file
    client = _authed_client(monkeypatch, tmp_path)
    assert client.get("/api/artifacts/mesh/ds1/sub").status_code == 404


def test_reject_unsafe_segment_blocks_traversal():
    from fastapi import HTTPException

    from app.routers.artifacts import _reject_unsafe_segment

    # good names pass
    _reject_unsafe_segment("ds1")
    _reject_unsafe_segment("surface_t0000.vtp")

    # traversal / separators / bad chars are rejected with 400
    # …and so is anything dot-prefixed, which keeps the cache's `.index/` unreachable
    for bad in ["..", "../etc", "a/b", "a\\b", "..%2f", "foo bar", "", ".index", ".hidden"]:
        with pytest.raises(HTTPException) as exc:
            _reject_unsafe_segment(bad)
        assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Converter round-trip — VTK-only (skipped where VTK isn't installed)
# --------------------------------------------------------------------------- #


def test_stl_conversion_roundtrip_and_cache(tmp_path):
    vtk = pytest.importorskip("vtk")
    from app.services.mesh.stl import convert_stl_sync

    # Synthesize a small binary STL from a sphere.
    sphere = vtk.vtkSphereSource()
    sphere.Update()
    stl_path = tmp_path / "sphere.stl"
    writer = vtk.vtkSTLWriter()
    writer.SetFileName(str(stl_path))
    writer.SetInputConnection(sphere.GetOutputPort())
    writer.Write()

    out = tmp_path / "out"
    metadata = convert_stl_sync(stl_path, out)

    assert (out / "surface_t0000.vtp").is_file()
    assert (out / "manifest.json").is_file()
    assert (out / "meta.json").is_file()
    assert metadata["num_timesteps"] == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["files"][0]["filename"] == "surface_t0000.vtp"
    assert manifest["variables"] == {"point": [], "cell": []}


def test_convert_sync_stl_branch_uses_cache(tmp_path):
    pytest.importorskip("vtk")
    import vtk

    from app.services.mesh.cache import compute_dataset_id

    sphere = vtk.vtkSphereSource()
    sphere.Update()
    stl_path = tmp_path / "sphere.stl"
    writer = vtk.vtkSTLWriter()
    writer.SetFileName(str(stl_path))
    writer.SetInputConnection(sphere.GetOutputPort())
    writer.Write()

    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    dataset_id = service._convert_sync(stl_path, cache_root, "sphere.stl", "", "model/stl")
    assert dataset_id == compute_dataset_id(stl_path, {"type": "stl"})
    assert (cache_root / dataset_id / "surface_t0000.vtp").is_file()
    props = service._props_from_cache(cache_root, dataset_id, "sphere.stl")
    assert props is not None
    assert props["metadata"]["num_elements"] > 0

    # Second call is a cache hit (manifest already present) — same id, still valid.
    dataset_id2 = service._convert_sync(stl_path, cache_root, "sphere.stl", "", "model/stl")
    assert dataset_id2 == dataset_id
