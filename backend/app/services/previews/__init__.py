"""Previews: processed, viewer-ready renditions of artifacts, built on demand and cached.

Some artifacts can't be shown from their raw bytes — an Exodus mesh has to become
``.vtp`` timesteps before the browser can draw it. A *previewer* (:class:`Previewer`)
knows how to turn one kind of artifact into such a rendition. The platform itself
knows nothing about any particular kind:

- ``registry`` lists the previewers and picks the one (if any) for an artifact.
  ``ArtifactMeta.preview_url`` is set exactly when one applies.
- ``service`` builds a preview the first time it is asked for (download the source,
  run the previewer in a subprocess pool), caches the result on disk, and dedupes
  concurrent requests. Everything cached is served back by one generic file route.

Adding a previewer means writing a :class:`Previewer` and adding it to
``registry.PREVIEWERS``; routes, caching, the wire field, and the frontend's lazy
loading all come for free.
"""

from .base import PreviewFailed, Previewer, PreviewUnavailable, PreviewUnreadable

__all__ = ["PreviewFailed", "Previewer", "PreviewUnavailable", "PreviewUnreadable"]
