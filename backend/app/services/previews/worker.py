"""The child-process half of a preview build. Imports only the standard library.

:func:`build_preview` runs in a spawned worker of the build pool (``pool.py``). A
spawned child imports the module its callable lives in, so this module stays free of
the app (no ``app.config``, no Redis, no FastAPI): the child loads this, the
previewer's own module (pickled by reference), and whatever the previewer imports
lazily inside ``build`` — VTK for the mesh previewer, and nothing else.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

PROPS_FILE = "preview.json"


def read_props(preview_dir: Path) -> Optional[dict[str, Any]]:
    """A built preview's props, or ``None`` if ``preview_dir`` isn't a complete build."""
    try:
        props = json.loads((preview_dir / PROPS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return props if isinstance(props, dict) else None


def build_preview(previewer: Any, source: Path, artifact: dict[str, Any], root: Path, key: str) -> None:
    """Build into a scratch directory, then rename it into place as ``root/key``.

    Another process (a second web worker, or a pool worker whose build we timed out on)
    may finish the same key first. Their build is as good as ours, so ours is dropped
    rather than replacing a directory a viewer may be reading from.
    """
    scratch = root / f".build-{key}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir()
    try:
        props = previewer.build(source, artifact, scratch)
        (scratch / PROPS_FILE).write_text(json.dumps(props))
        final = root / key
        if read_props(final) is not None:
            return
        if final.exists():
            # Left over from a build whose props file is gone (pruned by hand).
            shutil.rmtree(final, ignore_errors=True)
        try:
            os.rename(scratch, final)
        except OSError:
            if read_props(final) is None:  # lost a race to a complete build → fine
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
