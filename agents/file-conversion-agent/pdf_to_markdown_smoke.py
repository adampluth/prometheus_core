#!/usr/bin/env python3
"""Local smoke-test entrypoint for PDF to Markdown conversion."""

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from agent import DEFAULT_OPENDATALOADER_TIMEOUT_SECONDS, PdfToMarkdownWorker


class LocalPdfToMarkdownWorker(PdfToMarkdownWorker):
    """Run the production worker without requiring an agent request context."""

    def emit_status_update(self, message: str) -> None:
        print(message, file=sys.stderr)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a local PDF file to Markdown using the file-conversion-agent backend."
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the input PDF inside the container.")
    parser.add_argument(
        "output_markdown",
        type=Path,
        help="Path where the Markdown output should be written inside the container.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_OPENDATALOADER_TIMEOUT_SECONDS,
        help=f"Conversion timeout in seconds. Default: {DEFAULT_OPENDATALOADER_TIMEOUT_SECONDS}.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    source_pdf = args.input_pdf
    output_markdown = args.output_markdown

    if not source_pdf.exists():
        print(f"Input PDF does not exist: {source_pdf}", file=sys.stderr)
        return 2
    if source_pdf.suffix.lower() != ".pdf":
        print(f"Input file must have a .pdf extension: {source_pdf}", file=sys.stderr)
        return 2

    worker = LocalPdfToMarkdownWorker(
        source_pdf=source_pdf,
        output_markdown=output_markdown,
        timeout_seconds=args.timeout_seconds,
    )
    result = worker.run()
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "converted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
