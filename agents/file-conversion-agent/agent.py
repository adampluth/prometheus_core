"""
File Conversion Agent.

Converts reusable conversation artifacts from one file format to another. The
first supported conversion is PDF to Markdown.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from a2a.server.agent_execution.context import RequestContext
from a2a.server.events import EventQueue
from prometheus_api.agent import PrometheusAgent, PrometheusWorker
from prometheus_api.storage_client.connection import ConnectionConfig


SKILL_PDF_TO_MARKDOWN = "pdf_to_markdown"
MARKDOWN_MIME_TYPE = "text/markdown"
DEFAULT_MARKER_TIMEOUT_SECONDS = 900
WORKER_POLL_INTERVAL_SECONDS = 1.0


def _try_storage_config() -> Optional[ConnectionConfig]:
    """Return a ConnectionConfig from env, or None if credentials are missing."""
    try:
        return ConnectionConfig.from_env()
    except ValueError:
        return None


class PdfToMarkdownWorker(PrometheusWorker):
    """Run Marker PDF conversion off the event loop."""

    def __init__(
        self,
        *,
        source_pdf: Path,
        output_markdown: Path,
        timeout_seconds: int = DEFAULT_MARKER_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self.source_pdf = source_pdf
        self.output_markdown = output_markdown
        self.timeout_seconds = timeout_seconds

    def run(self) -> Dict[str, Any]:
        start = time.monotonic()
        self.emit_status_update("Starting PDF to Markdown conversion with marker-pdf.")

        if not self.source_pdf.exists():
            raise RuntimeError(f"PDF file does not exist: {self.source_pdf}")

        result = self._run_marker_subprocess(start)
        if result.get("status") == "cancelled":
            return result

        if not self.output_markdown.exists() or self.output_markdown.stat().st_size == 0:
            raise RuntimeError("marker-pdf did not produce a Markdown file.")

        elapsed = int(time.monotonic() - start)
        self.emit_status_update(f"PDF to Markdown conversion finished in {elapsed}s.")
        return {
            "status": "converted",
            "conversion_backend": "marker-pdf",
            "elapsed_seconds": elapsed,
            "output_path": str(self.output_markdown),
        }

    def _run_marker_subprocess(self, start: float) -> Dict[str, Any]:
        script = """
import os
import sys
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["TORCH_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.output import text_from_rendered

source_pdf = Path(sys.argv[1])
output_markdown = Path(sys.argv[2])

converter = PdfConverter(artifact_dict=create_model_dict())
rendered = converter(str(source_pdf))
text, _, _ = text_from_rendered(rendered)
output_markdown.write_text(text or "", encoding="utf-8")
"""

        env = {
            **os.environ,
            "CUDA_VISIBLE_DEVICES": "",
            "TORCH_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(self.source_pdf), str(self.output_markdown)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        last_status = start
        while proc.poll() is None:
            elapsed = time.monotonic() - start
            if self.cancelled:
                self._terminate_process(proc)
                return {
                    "status": "cancelled",
                    "conversion_backend": "marker-pdf",
                    "elapsed_seconds": int(elapsed),
                }
            if elapsed > self.timeout_seconds:
                self._terminate_process(proc)
                raise RuntimeError(
                    f"marker-pdf timed out after {self.timeout_seconds}s."
                )
            if time.monotonic() - last_status >= 10:
                self.emit_status_update(
                    f"Converting PDF to Markdown... {int(elapsed)}s elapsed."
                )
                last_status = time.monotonic()
            time.sleep(1)

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            details = (stderr or stdout or "").strip()
            if len(details) > 4000:
                details = details[-4000:]
            raise RuntimeError(f"marker-pdf failed: {details or 'unknown error'}")

        return {"status": "converted"}

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


class FileConversionAgent(PrometheusAgent):
    """Artifact-aware file conversion agent."""

    @staticmethod
    def _sanitize_stem(value: str, fallback: str = "converted") -> str:
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
        candidate = candidate.strip("._-")
        return candidate or fallback

    @staticmethod
    def _resolve_artifact_id(params: Mapping[str, Any]) -> str:
        artifact_id = str(params.get("artifact_id") or "").strip()
        if artifact_id:
            return artifact_id

        upstream = params.get("upstream_results") or {}
        if isinstance(upstream, Mapping):
            for skill_result in upstream.values():
                if not isinstance(skill_result, Mapping):
                    continue
                candidate = (
                    skill_result.get("artifact_id")
                    or (skill_result.get("artifact") or {}).get("artifact_id")
                    or ""
                )
                if candidate:
                    return str(candidate)
        return ""

    @staticmethod
    def _find_catalog_artifact(
        artifacts: Optional[Sequence[Mapping[str, Any]]],
        artifact_id: str,
    ) -> Optional[Dict[str, Any]]:
        for item in artifacts or []:
            if str(item.get("artifact_id") or "") == artifact_id:
                return dict(item)
        return None

    @staticmethod
    def _is_pdf_artifact(source_path: Path, source: Mapping[str, Any]) -> bool:
        mime_type = str(source.get("mime_type") or "").lower()
        name = str(source.get("name") or source_path.name)
        return mime_type == "application/pdf" or Path(name).suffix.lower() == ".pdf"

    @staticmethod
    def _markdown_name(params: Mapping[str, Any], source_name: str) -> str:
        requested_name = str(params.get("name") or "").strip()
        base = Path(requested_name).stem if requested_name else Path(source_name).stem
        return f"{FileConversionAgent._sanitize_stem(base)}.md"

    async def _execute(
        self,
        params: Dict[str, Any],
        context: RequestContext,
        event_queue: EventQueue,
    ) -> Dict[str, Any]:
        skill_id = str(params.get("skill_id") or SKILL_PDF_TO_MARKDOWN).strip()
        if skill_id != SKILL_PDF_TO_MARKDOWN:
            return {
                "status": "error",
                "message": f"Unknown file conversion skill '{skill_id}'.",
            }
        return await self._handle_pdf_to_markdown(params, context, event_queue)

    async def _handle_pdf_to_markdown(
        self,
        params: Dict[str, Any],
        context: RequestContext,
        event_queue: EventQueue,
    ) -> Dict[str, Any]:
        conversation_id = str(params.get("conversation_id") or context.context_id or str(uuid.uuid4()))

        storage_config = _try_storage_config()
        if storage_config is None:
            return {
                "status": "error",
                "conversation_id": conversation_id,
                "message": "DEEPLYNX_* credentials not configured.",
            }

        artifact_id = self._resolve_artifact_id(params)
        if not artifact_id:
            return {
                "status": "error",
                "conversation_id": conversation_id,
                "message": "Parameter 'artifact_id' is required for PDF to Markdown conversion.",
            }

        artifact_catalog = (
            params.get("artifacts") if isinstance(params.get("artifacts"), list) else None
        )
        try:
            source_pdf = await self.get_artifact_file_path(
                artifact_id,
                context=context,
                conversation_id=conversation_id,
                artifacts=artifact_catalog,
            )
        except FileNotFoundError:
            return {
                "status": "error",
                "conversation_id": conversation_id,
                "message": f"Artifact '{artifact_id}' was not found in this conversation.",
            }
        except Exception as exc:
            return {
                "status": "error",
                "conversation_id": conversation_id,
                "message": f"Failed to retrieve artifact '{artifact_id}' as a local file: {exc}",
            }

        source_ref = self.resolve_artifact_reference(conversation_id, artifact_id)
        source_dict = source_ref.to_dict() if source_ref else {}
        if not source_dict:
            source_dict = self._find_catalog_artifact(artifact_catalog, artifact_id) or {}
        source_name = str(source_dict.get("name") or source_pdf.name)
        source_dict.setdefault("artifact_id", artifact_id)
        source_dict.setdefault("name", source_name)

        if not self._is_pdf_artifact(source_pdf, source_dict):
            return {
                "status": "error",
                "conversation_id": conversation_id,
                "message": f"Artifact '{artifact_id}' is not a PDF file.",
                "source_artifact": source_dict,
            }

        markdown_name = self._markdown_name(params, source_name)
        description = str(
            params.get("description")
            or f"Markdown converted from PDF artifact '{source_name}'."
        )

        await self.send_status_update(
            context=context,
            event_queue=event_queue,
            message=f"Resolving PDF artifact '{source_name}' as a local file.",
        )

        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / markdown_name
            worker = PdfToMarkdownWorker(
                source_pdf=source_pdf,
                output_markdown=markdown_path,
                timeout_seconds=DEFAULT_MARKER_TIMEOUT_SECONDS,
            )

            try:
                conversion_result = await self.run_worker(
                    worker,
                    context,
                    event_queue,
                    poll_interval=WORKER_POLL_INTERVAL_SECONDS,
                )
            except Exception as exc:
                return {
                    "status": "error",
                    "conversation_id": conversation_id,
                    "message": f"Failed to convert PDF artifact '{source_name}' to Markdown: {exc}",
                    "source_artifact": source_dict,
                }

            if conversion_result.get("status") == "cancelled":
                return {
                    "status": "cancelled",
                    "conversation_id": conversation_id,
                    "message": f"PDF to Markdown conversion cancelled for '{source_name}'.",
                    "source_artifact": source_dict,
                    "conversion_backend": conversion_result.get("conversion_backend"),
                }

            await self.send_status_update(
                context=context,
                event_queue=event_queue,
                message="Uploading Markdown artifact to DeepLynx.",
            )

            output_ref = await self.send_deeplynx_artifact(
                context=context,
                event_queue=event_queue,
                file_path=markdown_path,
                name=markdown_name,
                description=description,
                storage_config=storage_config,
                mime_type=MARKDOWN_MIME_TYPE,
                tags=["markdown", "converted", "pdf-to-markdown"],
                metadata={
                    "source_artifact_id": artifact_id,
                    "source_artifact_name": source_name,
                    "conversion_backend": conversion_result.get("conversion_backend", "marker-pdf"),
                },
            )

        await self.send_status_update(
            context=context,
            event_queue=event_queue,
            message=f"Created Markdown artifact {output_ref.artifact_id}.",
        )
        await self.send_conversation_message(
            context=context,
            event_queue=event_queue,
            message="PDF artifact converted to Markdown successfully.",
            metadata={"artifact_id": output_ref.artifact_id},
        )

        return {
            "status": "converted",
            "conversation_id": conversation_id,
            "message": "PDF artifact converted to Markdown successfully.",
            "source_artifact": source_dict,
            "artifact": output_ref.to_dict(),
            "artifact_id": output_ref.artifact_id,
            "artifact_name": output_ref.name,
            "artifact_uri": output_ref.uri,
            "conversion_backend": conversion_result.get("conversion_backend", "marker-pdf"),
        }


if __name__ == "__main__":
    agent = FileConversionAgent(agent_name="File Conversion Agent")
    agent.run(port=int(os.getenv("AGENT_PORT", "8000")))
