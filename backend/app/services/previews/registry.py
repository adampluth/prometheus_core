"""The installed previewers, and which one (if any) handles an artifact."""

from __future__ import annotations

from typing import Any, Optional

from app.services.mesh.preview import MeshPreviewer

from .base import Previewer

# First match wins.
PREVIEWERS: tuple[Previewer, ...] = (MeshPreviewer(),)


def previewer_for(artifact: dict[str, Any], render_hint: str) -> Optional[Previewer]:
    """The previewer for ``artifact``, or ``None`` when it is viewed from its raw bytes."""
    for previewer in PREVIEWERS:
        if previewer.applies(artifact, render_hint):
            return previewer
    return None
