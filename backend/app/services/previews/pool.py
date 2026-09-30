"""The process pool preview builds run in, so a crashing previewer costs one preview.

WHY A SUBPROCESS AND NOT A THREAD: previewers wrap large native libraries (VTK for
meshes). A segfault or an OOM kill inside one takes down the whole interpreter — with
a thread pool that is the web process, dropping every in-flight conversation. In a
child process the same crash fails exactly one preview, which the viewer shows with a
retry and a download link.

The child side is ``worker.py``; everything a build sends across (the previewer,
paths, the artifact dict) must pickle, which ``tests/test_previews.py`` guards.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Optional

from app.config import settings

from .base import Previewer, PreviewFailed
from .worker import build_preview

logger = logging.getLogger(__name__)

# Built lazily so importing this module costs nothing, and dropped whenever a child
# dies so the next build rebuilds it. All access happens from the event-loop thread
# (every caller is a coroutine and none of the accessors await), so the bare global
# needs no lock.
_executor: Optional[ProcessPoolExecutor] = None


def _pool() -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        # "spawn", never "fork": forking a live asyncio server — with its selector,
        # its httpx/redis connection pools and its threads — into a child that then
        # loads VTK is undefined behaviour. spawn starts a clean interpreter that
        # imports only what worker.build_preview needs.
        #
        # max_tasks_per_child=1 retires each worker after one build. VTK does not
        # reliably release memory between datasets, so a long-lived worker ratchets
        # upward until it hits the container limit; a fresh process per build bounds
        # that at one source. The cost is one spawn + library import (order of a
        # second) per build, paid only on a cache miss.
        _executor = ProcessPoolExecutor(
            max_workers=settings.preview_workers,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=1,
        )
    return _executor


def _discard(executor: ProcessPoolExecutor, *, kill_children: bool) -> None:
    """Drop ``executor`` so the next :func:`_pool` call builds a fresh one.

    Acts only while ``executor`` is still *the* pool. A build names the pool it ran on,
    because by the time it fails that pool may already have been replaced: a timeout
    kills pool A, a new build creates pool B, and another job still on A then gets
    ``BrokenProcessPool``. Discarding "whatever is current" there would shut down B and
    cancel unrelated builds queued on it.

    A :class:`BrokenProcessPool` poisons the executor for *every* later submit, so it
    has to be replaced, not reused.

    ``kill_children`` is for the timeout path. ``shutdown(cancel_futures=True)``
    cancels work that has not started but cannot interrupt a call already running in
    a child, so a wedged build would keep burning a core indefinitely; terminating the
    workers is the only way to stop it. That can also abort a *different* build that
    happened to be running concurrently — it fails like any other build, and its
    viewer offers a retry.
    """
    global _executor
    if _executor is not executor:
        return  # already replaced; whoever replaced it dealt with this one
    _executor = None
    if kill_children:
        # `_processes` is private, so guard it: on a CPython that renames it we still
        # get the cancel-pending-work half below, just not the hard kill.
        for proc in list((getattr(executor, "_processes", None) or {}).values()):
            try:
                if proc.is_alive():
                    proc.kill()
            except Exception:  # pragma: no cover - best-effort teardown
                logger.debug("previews: could not kill worker %r", proc, exc_info=True)
    executor.shutdown(wait=False, cancel_futures=True)


def shutdown() -> None:
    """Tear the pool down on app shutdown (called from the ``main.py`` lifespan).

    Without this, spawned workers outlive the server on a graceful stop and the
    container lingers until Docker's kill timeout expires.
    """
    if _executor is not None:
        _discard(_executor, kill_children=True)


async def run_build(previewer: Previewer, source: Path, artifact: dict[str, Any], root: Path, key: str) -> None:
    """Run :func:`worker.build_preview` in the pool, bounded by a timeout.

    Errors the previewer raises (``PreviewUnreadable``, …) come back through the
    future unchanged. The pool's own failures become :class:`PreviewFailed`:

    * :class:`BrokenProcessPool` — the worker died (a native crash, or the kernel
      OOM-killed it). The pool is replaced.
    * timeout — a wedged build can't be cancelled cooperatively, so the workers are
      killed (see :func:`_discard`).
    * a teardown cancelling our queued job arrives as :class:`asyncio.CancelledError`;
      unlike a real cancellation of this task, it must not propagate as one.
      ``Task.cancelling()`` tells the two apart.
    """
    loop = asyncio.get_running_loop()
    executor = _pool()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(executor, build_preview, previewer, source, artifact, root, key),
            timeout=settings.preview_timeout_seconds,
        )
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise  # we are being cancelled; not the pool's doing
        logger.warning("previews: %s build %s was cancelled by a pool teardown", previewer.name, key)
        raise PreviewFailed("the build was interrupted; try again") from None
    except BrokenProcessPool:
        logger.warning("previews: %s build %s crashed its worker; rebuilding the pool", previewer.name, key)
        _discard(executor, kill_children=False)
        raise PreviewFailed("the preview builder crashed (the file may be too large or malformed)") from None
    except asyncio.TimeoutError:
        logger.warning(
            "previews: %s build %s exceeded %ss; killing the pool",
            previewer.name,
            key,
            settings.preview_timeout_seconds,
        )
        _discard(executor, kill_children=True)
        raise PreviewFailed(f"building the preview took longer than {settings.preview_timeout_seconds}s") from None
