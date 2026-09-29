"""The previewer interface and the errors a preview build can raise. Dependency-free,
so previewer modules can import it without pulling in the rest of the app."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class PreviewUnavailable(Exception):
    """The artifact's source could not be fetched (mapped to 502)."""


class PreviewUnreadable(Exception):
    """The source was fetched but the previewer can't process it (mapped to 422)."""


class Previewer(Protocol):
    """Turns one kind of artifact into a viewer-ready rendition.

    ``name`` and ``version`` are part of the cache key: bump ``version`` whenever
    :meth:`build` would produce different output, and old previews stop matching.
    """

    name: str
    version: str

    def applies(self, artifact: dict[str, Any], render_hint: str) -> bool:
        """Whether this previewer handles ``artifact``. Must be cheap: it runs for
        every artifact shaped onto the wire."""
        ...

    def build(self, source: Path, artifact: dict[str, Any], out_dir: Path) -> dict[str, Any]:
        """Write the preview's files into ``out_dir`` and return the viewer props.

        Runs in a worker thread, so it may block (and may import heavy libraries
        lazily). ``source`` is the artifact's resolved local file. The returned
        props must be JSON-serializable; the service adds ``baseUrl``, under which
        every file in ``out_dir`` is served by name. Raise :class:`PreviewUnreadable`
        for input the previewer can't handle.
        """
        ...
