"""Tests for the generic preview service and file route (``app/services/previews``).

A fake previewer stands in for real ones, so these run offline: build on first
request, warm hits, rebuilds, in-flight dedupe, failure + retry, the subprocess
pool's crash/timeout containment, and the ``/api/previews/{key}/{filename}``
route's auth and path-traversal guards.
"""

import asyncio
import json
import pickle
import time
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import uri_resolver
from app.services.previews import PreviewFailed, PreviewUnavailable, PreviewUnreadable
from app.services.previews import pool, service, worker
from app.services.previews.registry import PREVIEWERS


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


@pytest.fixture
def in_process_pool(monkeypatch):
    """Run builds on a thread instead of a spawned child, so the service tests can
    count calls on the previewer they hold. The subprocess path has its own tests
    below."""
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(pool, "_executor", executor)
    yield
    executor.shutdown(wait=True)


_ART = {"artifact_id": "a1", "name": "thing.bin", "uri": "file:///x/thing.bin"}


async def test_builds_on_first_request(monkeypatch, tmp_path, cache_root, in_process_pool):
    _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer()

    props = await service.preview_props(previewer, dict(_ART))

    key = service.preview_key(previewer, _ART["uri"])
    assert len(previewer.builds) == 1
    assert props == {"size": len(b"source-bytes"), "baseUrl": f"/api/previews/{key}"}
    assert (cache_root / key / "data.bin").read_bytes() == b"source-bytes"
    assert json.loads((cache_root / key / "preview.json").read_text()) == {"size": 12}


async def test_warm_hit_skips_download_and_build(monkeypatch, tmp_path, cache_root, in_process_pool):
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


async def test_rebuilds_when_the_preview_is_gone(monkeypatch, tmp_path, cache_root, in_process_pool):
    import shutil

    _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer()
    await service.preview_props(previewer, dict(_ART))
    shutil.rmtree(cache_root / service.preview_key(previewer, _ART["uri"]))

    await service.preview_props(previewer, dict(_ART))

    assert len(previewer.builds) == 2


async def test_dedupes_concurrent_requests(monkeypatch, tmp_path, cache_root, in_process_pool):
    downloads = _stub_resolver(monkeypatch, tmp_path)
    previewer = _FakePreviewer(delay=0.1)

    results = await asyncio.gather(*(service.preview_props(previewer, dict(_ART)) for _ in range(3)))

    assert len(downloads) == 1 and len(previewer.builds) == 1
    assert len({r["baseUrl"] for r in results}) == 1


async def test_failure_propagates_leaves_nothing_behind_and_is_retried(
    monkeypatch, tmp_path, cache_root, in_process_pool
):
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


async def test_losing_a_build_race_keeps_the_winners_preview(tmp_path):
    """Two web workers can build one cold preview; the second rename must not replace
    a complete build a viewer may already be reading."""
    root = tmp_path / "previews"
    root.mkdir()
    src = tmp_path / "source.bin"
    src.write_bytes(b"source-bytes")
    (root / "k1").mkdir()
    (root / "k1" / worker.PROPS_FILE).write_text(json.dumps({"winner": True}))

    worker.build_preview(_FakePreviewer(), src, dict(_ART), root, "k1")

    assert worker.read_props(root / "k1") == {"winner": True}
    assert [p.name for p in root.iterdir()] == ["k1"]  # our scratch dir is gone


# --------------------------------------------------------------------------- #
# pool.run_build — subprocess crash + timeout containment
# --------------------------------------------------------------------------- #


class _FakeProc:
    """Stand-in for a ProcessPoolExecutor worker process."""

    def __init__(self) -> None:
        self.killed = False

    def is_alive(self) -> bool:
        return not self.killed

    def kill(self) -> None:
        self.killed = True


class _FakeExecutor:
    """Minimal executor: ``run_in_executor`` only needs ``.submit()`` returning a Future.

    ``_processes`` mirrors ProcessPoolExecutor's private worker map, which
    ``pool._discard(..., kill_children=True)`` reaches into to stop a wedged build.
    """

    def __init__(self, future: futures.Future) -> None:
        self._future = future
        self._processes = {1: _FakeProc()}
        self.shutdown_calls: list[dict] = []

    def submit(self, fn, *args):
        return self._future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_calls.append({"wait": wait, "cancel_futures": cancel_futures})


def _run(tmp_path):
    return pool.run_build(_FakePreviewer(), tmp_path / "s.bin", dict(_ART), tmp_path, "k")


async def test_run_build_survives_a_dead_worker(monkeypatch, tmp_path):
    """A child segfault/OOM must cost one preview, not the web process."""
    future: futures.Future = futures.Future()
    future.set_running_or_notify_cancel()
    future.set_exception(BrokenProcessPool("worker died"))
    fake = _FakeExecutor(future)
    monkeypatch.setattr(pool, "_executor", fake)

    with pytest.raises(PreviewFailed, match="crashed"):
        await _run(tmp_path)

    # The pool is poisoned for every later submit, so it must be replaced, not reused.
    assert pool._executor is None
    assert fake.shutdown_calls == [{"wait": False, "cancel_futures": True}]
    # A dead worker needs no killing; only the timeout path does that.
    assert fake._processes[1].killed is False


async def test_run_build_times_out_and_kills_the_pool(monkeypatch, tmp_path):
    """A wedged build can't be cancelled cooperatively, so the workers are killed."""
    fake = _FakeExecutor(futures.Future())  # never completes
    monkeypatch.setattr(pool, "_executor", fake)
    monkeypatch.setattr(pool.settings, "preview_timeout_seconds", 0.05, raising=False)

    with pytest.raises(PreviewFailed, match="longer than"):
        await _run(tmp_path)

    assert pool._executor is None
    assert fake._processes[1].killed is True
    assert fake.shutdown_calls == [{"wait": False, "cancel_futures": True}]


async def test_stale_pool_failure_leaves_the_replacement_pool_alone(monkeypatch, tmp_path):
    """A job still on a replaced pool A breaks with ``BrokenProcessPool`` after B took
    over; only the pool the job actually ran on may be discarded."""
    future: futures.Future = futures.Future()
    stale = _FakeExecutor(future)
    replacement = _FakeExecutor(futures.Future())
    monkeypatch.setattr(pool, "_executor", stale)

    async def fail_after_replacement():
        await asyncio.sleep(0)  # let run_build submit to `stale` first
        pool._executor = replacement  # a timeout elsewhere replaced the pool
        future.set_running_or_notify_cancel()
        future.set_exception(BrokenProcessPool("worker died"))

    results = await asyncio.gather(_run(tmp_path), fail_after_replacement(), return_exceptions=True)

    assert isinstance(results[0], PreviewFailed)
    assert pool._executor is replacement
    assert replacement.shutdown_calls == []


async def test_pool_teardown_cancellation_fails_the_build_instead_of_propagating(monkeypatch, tmp_path):
    """``shutdown(cancel_futures=True)`` cancels our pending job. That is not a
    cancellation of *this* task, so it must surface as a failed preview, not escape
    as a CancelledError."""
    future: futures.Future = futures.Future()
    monkeypatch.setattr(pool, "_executor", _FakeExecutor(future))

    async def teardown():
        await asyncio.sleep(0)
        future.cancel()

    results = await asyncio.gather(_run(tmp_path), teardown(), return_exceptions=True)
    assert isinstance(results[0], PreviewFailed)


async def test_real_task_cancellation_still_propagates(monkeypatch, tmp_path):
    monkeypatch.setattr(pool, "_executor", _FakeExecutor(futures.Future()))
    task = asyncio.create_task(_run(tmp_path))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_shutdown_is_a_noop_when_unused(monkeypatch):
    """The lifespan calls this unconditionally; a server that built nothing has no pool."""
    monkeypatch.setattr(pool, "_executor", None)
    pool.shutdown()  # must not raise
    assert pool._executor is None


@pytest.mark.parametrize("previewer", PREVIEWERS, ids=lambda p: p.name)
def test_registered_previewers_cross_the_process_boundary(previewer):
    """Every previewer is pickled into the build child. One that closes over something
    unpicklable would otherwise only fail at runtime, inside a submit."""
    clone = pickle.loads(pickle.dumps(previewer))
    assert (clone.name, clone.version) == (previewer.name, previewer.version)
    assert pickle.loads(pickle.dumps(worker.build_preview)) is worker.build_preview


def test_worker_module_stays_free_of_the_app():
    """The spawned child imports ``worker.py``; pulling the app in there would load
    config, Redis, and FastAPI into every build process."""
    source = Path(worker.__file__).read_text()
    assert "import app" not in source and "from app" not in source


async def test_real_subprocess_build_end_to_end(monkeypatch, tmp_path, cache_root):
    """One genuine spawn: the build runs in a child and its files land in the cache."""
    _stub_resolver(monkeypatch, tmp_path)
    monkeypatch.setattr(pool, "_executor", None)
    try:
        props = await service.preview_props(_FakePreviewer(), dict(_ART))
    finally:
        pool.shutdown()
    key = service.preview_key(_FakePreviewer(), _ART["uri"])
    assert props["size"] == 12
    assert (cache_root / key / "data.bin").read_bytes() == b"source-bytes"


# --------------------------------------------------------------------------- #
# GET /api/previews/{key}/{filename}
# --------------------------------------------------------------------------- #


def _client(monkeypatch, tmp_path, *, authed=True) -> TestClient:
    from app import security
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "preview_cache_dir", str(tmp_path), raising=False)

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
