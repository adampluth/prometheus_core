"""Public sync API for the Exodus II converter.

Ported from the old ``frontend/components/exodus/api.py``, keeping the synchronous
:func:`convert_exodus` / :func:`inspect_exodus`. The mesh previewer
(:mod:`app.services.mesh.preview`) drives both the Exodus and STL paths; the generic
preview service (:mod:`app.services.previews`) owns the thread pool and the cache. ``import vtk`` transitively via :mod:`vtk_pipeline` — import lazily.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .cache import compute_dataset_id, get_cache_path, is_cached
from .exceptions import InvalidExodusFileError
from .models import ConvertRequest, ConvertResult
from .vtk_pipeline import create_exodus_reader, extract_metadata, run_conversion


def inspect_exodus(input_path: Path) -> dict:
    """Read an Exodus II file's metadata (timesteps, variables, blocks, bounds)."""
    if isinstance(input_path, str):
        input_path = Path(input_path)
    if not input_path.exists():
        raise InvalidExodusFileError(f"File not found: {input_path}")
    try:
        reader = create_exodus_reader(input_path)
        return extract_metadata(reader).to_dict()
    except Exception as exc:
        raise InvalidExodusFileError(f"Failed to read Exodus file: {exc}") from exc


def convert_exodus(request: ConvertRequest, cache_root: Optional[Path] = None) -> ConvertResult:
    """Convert an Exodus II file to web-friendly VTP.

    When ``cache_root`` is given, the output directory is ``cache_root/<dataset_id>``
    and an already-converted dataset (``manifest.json`` present) is reused without
    re-running VTK.
    """
    dataset_id = compute_dataset_id(request.input_path, asdict(request))

    if cache_root:
        output_dir = get_cache_path(cache_root, dataset_id)
        if is_cached(cache_root, dataset_id):
            with open(output_dir / "meta.json") as f:
                metadata = json.load(f)
            return ConvertResult(
                dataset_id=dataset_id,
                output_dir=output_dir,
                manifest_path=output_dir / "manifest.json",
                metadata=metadata,
            )
    else:
        output_dir = request.output_dir

    request.output_dir = output_dir
    run_conversion(request)

    with open(output_dir / "meta.json") as f:
        metadata = json.load(f)

    return ConvertResult(
        dataset_id=dataset_id,
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        metadata=metadata,
    )
