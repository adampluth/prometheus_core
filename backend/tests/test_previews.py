"""Tests for the generic preview service and file route (``app/services/previews``).

A fake previewer stands in for real ones, so these run offline: build on first
request, warm hits, rebuilds, in-flight dedupe, failure + retry, and the
``/api/previews/{key}/{filename}`` route's auth and path-traversal guards.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import uri_resolver
from app.services.previews import PreviewUnavailable, PreviewUnreadable
from app.services.previews import service


class _FakePreviewer:
    name = "fake"
    version = "1"

    def __init__(self, *, delay=0.0, fail=None):
        self.delay = delay
        self.fail = fail
        self.builds = []

    def applies(self, artifact, render_hint):
        return True

    def build(self, source, artifact, out_dir):
        self.builds.append(source)
        time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        (out_dir / "data.bin").write_bytes(source.read_bytes())
        return {"size": source.stat().st_size}


def _stub_resolver(monkeypatch, tmp_path):
    """Make resolve_to_local_path return a real (existing) local file; count calls."""
    src = tmp_path / "source.bin"
    src.write_bytes(b"source-bytes")
    calls = []

    async def _resolve(uri, *, cache_dir, artifact=None, artifact_id=None):
        calls.append(uri)
        return src

    monkeypatch.setattr(uri_resolver, "resolve_to_local_path", _resolve)
    return calls


@pytest.fixture
def cache_root(monkeypatch, tmp_path):
    from app.config import settings

    root = tmp_path / "previews"
    monkeypatch.setattr(settings, "preview_cache_dir", str(root), raising=False)
    return root


_ART = {"artifact_id": "a1", "name": "thing.bin", "uri": "file:///x/thing.bin"}


async def test_builds_on_first_request(monkeypatch, tmp_path, cache_root):
    _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer()

    props = await service.preview_props(previewer, dict(_ART))

    key = service.preview_key(previewer, _ART["uri"])
    assert len(previewer.builds) == 1
    assert props == {"size": len(b"source-bytes"), "baseUrl": f"/api/previews/{key}"}
    assert (cache_root / key / "data.bin").read_bytes() == b"source-bytes"
    assert json.loads((cache_root / key / "preview.json").read_text()) == {"size": 12}


async def test_warm_hit_skips_download_and_build(monkeypatch, tmp_path, cache_root):
    downloads = _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer()
    first = await service.preview_props(previewer, dict(_ART))

    second = await service.preview_props(previewer, {**_ART, "name": "renamed.bin"})

    assert len(downloads) == 1 and len(previewer.builds) == 1
    assert second == first


async def test_key_depends_on_previewer_version_and_uri():
    a, b = _FakePreviewer(), _FakePreviewer()
    b.version = "2"
    assert service.preview_key(a, "u1") == service.preview_key(_FakePreviewer(), "u1")
    assert service.preview_key(a, "u1") != service.preview_key(b, "u1")
    assert service.preview_key(a, "u1") != service.preview_key(a, "u2")


async def test_rebuilds_when_the_preview_is_gone(monkeypatch, tmp_path, cache_root):
    import shutil

    _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer()
    await service.preview_props(previewer, dict(_ART))
    shutil.rmtree(cache_root / service.preview_key(previewer, _ART["uri"]))

    await service.preview_props(previewer, dict(_ART))

    assert len(previewer.builds) == 2


async def test_dedupes_concurrent_requests(monkeypatch, tmp_path, cache_root):
    downloads = _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer(delay=0.1)

    results = await asyncio.gather(*(service.preview_props(previewer, dict(_ART)) for _ in range(3)))

    assert len(downloads) == 1 and len(previewer.builds) == 1
    assert len({r["baseUrl"] for r in results}) == 1


async def test_failure_propagates_leaves_nothing_behind_and_is_retried(monkeypatch, tmp_path, cache_root):
    _stub_resolver(monkeypatch, tmp_path)
    failing = _FakePreviewer(fail=PreviewUnreadable("bad input"))

    with pytest.raises(PreviewUnreadable):
        await service.preview_props(failing, dict(_ART))
    assert list(cache_root.iterdir()) == []  # no scratch dir, no half-built preview

    working = _FakePreviewer()
    assert (await service.preview_props(working, dict(_ART)))["size"] == 12
    assert len(working.builds) == 1


async def test_unresolvable_source(monkeypatch, cache_root):
    async def _none(uri, *, cache_dir, artifact=None, artifact_id=None):
        return None

    monkeypatch.setattr(uri_resolver, "resolve_to_local_path", _none)
    with pytest.raises(PreviewUnavailable):
        await service.preview_props(_FakePreviewer(), dict(_ART))


async def test_requires_uri(cache_root):
    previewer = _FakePreviewer()
    with pytest.raises(PreviewUnavailable):
        await service.preview_props(previewer, {"artifact_id": "a2", "name": "thing.bin"})
    assert previewer.builds == []


# --------------------------------------------------------------------------- #
# GET /api/previews/{key}/{filename}
# --------------------------------------------------------------------------- #


def _client(monkeypatch, tmp_path, *, authed=True) -> TestClient:
    from app import security
    from app.config import settings
    from app.main import app
    from app.sessions import SessionStore

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "preview_cache_dir", str(tmp_path), raising=False)

    app.state.session_store = SessionStore()
    client = TestClient(app)
    if authed:
        token = security.issue_token(security.User(identifier="alice"), sid="sid-preview")
        client.cookies.set(settings.cookie_name, token)
    return client


def test_file_route_requires_auth(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, authed=False)
    assert client.get("/api/previews/k1/surface_t0000.vtp").status_code == 401


def test_file_route_serves_a_preview_file(monkeypatch, tmp_path):
    import app.services.mesh.preview  # noqa: F401 — registers the .vtp content type

    (tmp_path / "k1").mkdir()
    (tmp_path / "k1" / "surface_t0000.vtp").write_text("<VTKFile type='PolyData'></VTKFile>")

    resp = _client(monkeypatch, tmp_path).get("/api/previews/k1/surface_t0000.vtp")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "immutable" in resp.headers["cache-control"]
    assert "VTKFile" in resp.text


def test_file_route_404_when_missing(monkeypatch, tmp_path):
    assert _client(monkeypatch, tmp_path).get("/api/previews/k1/nope.vtp").status_code == 404


def test_file_route_rejects_directory_as_file(monkeypatch, tmp_path):
    (tmp_path / "k1" / "sub").mkdir(parents=True)
    assert _client(monkeypatch, tmp_path).get("/api/previews/k1/sub").status_code == 404


def test_file_route_cannot_reach_scratch_dirs(monkeypatch, tmp_path):
    (tmp_path / ".build-k1-abcd").mkdir()
    (tmp_path / ".build-k1-abcd" / "preview.json").write_text("{}")
    assert _client(monkeypatch, tmp_path).get("/api/previews/.build-k1-abcd/preview.json").status_code == 400


def test_reject_unsafe_segment_blocks_traversal():
    from fastapi import HTTPException

    from app.routers.artifacts import _reject_unsafe_segment

    _reject_unsafe_segment("k1")
    _reject_unsafe_segment("surface_t0000.vtp")

    for bad in ["..", "../etc", "a/b", "a\\b", "..%2f", "foo bar", "", ".build-x", ".hidden"]:
        with pytest.raises(HTTPException) as exc:
            _reject_unsafe_segment(bad)
        assert exc.value.status_code == 400
