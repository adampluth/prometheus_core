from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from a2a.server.agent_execution.context import RequestContext
from a2a.server.events import EventQueue


AGENT_DIR = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("file_conversion_agent_module", AGENT_DIR / "agent.py")
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

FileConversionAgent = _MODULE.FileConversionAgent


def _context() -> MagicMock:
    context = MagicMock(spec=RequestContext)
    context.task_id = "task-1"
    context.context_id = "conv-1"
    return context


def _artifact_ref(**overrides):
    data = {
        "artifact_id": "pdf-1",
        "conversation_id": "conv-1",
        "name": "report.pdf",
        "description": "Source report",
        "uri": "deeplynx://items/1",
        "mime_type": "application/pdf",
        "metadata": {},
    }
    data.update(overrides)

    return SimpleNamespace(
        artifact_id=data["artifact_id"],
        name=data["name"],
        uri=data["uri"],
        to_dict=lambda: dict(data),
    )


def test_pdf_to_markdown_requires_artifact_id(monkeypatch) -> None:
    agent = FileConversionAgent(agent_name="File Conversion Agent")
    monkeypatch.setattr(_MODULE, "_try_storage_config", lambda: object())

    result = asyncio.run(agent._execute({}, _context(), AsyncMock(spec=EventQueue)))

    assert result["status"] == "error"
    assert "artifact_id" in result["message"]


def test_pdf_to_markdown_rejects_non_pdf(monkeypatch, tmp_path: Path) -> None:
    agent = FileConversionAgent(agent_name="File Conversion Agent")
    source = tmp_path / "notes.txt"
    source.write_text("not a pdf", encoding="utf-8")

    async def get_file_path(*args, **kwargs):
        return source

    monkeypatch.setattr(_MODULE, "_try_storage_config", lambda: object())
    monkeypatch.setattr(agent, "get_artifact_file_path", get_file_path)
    monkeypatch.setattr(
        agent,
        "resolve_artifact_reference",
        lambda *args, **kwargs: _artifact_ref(name="notes.txt", mime_type="text/plain"),
    )

    result = asyncio.run(
        agent._execute({"artifact_id": "pdf-1"}, _context(), AsyncMock(spec=EventQueue))
    )

    assert result["status"] == "error"
    assert result["message"] == "Artifact 'pdf-1' is not a PDF file."


def test_pdf_to_markdown_uploads_markdown_artifact(monkeypatch, tmp_path: Path) -> None:
    agent = FileConversionAgent(agent_name="File Conversion Agent")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    captured = {}

    async def get_file_path(*args, **kwargs):
        return source

    async def run_worker(worker, context, event_queue, *, poll_interval):
        assert worker.source_pdf == source
        assert worker.output_markdown.name == "converted_report.md"
        worker.output_markdown.write_text("# Converted\n", encoding="utf-8")
        return {
            "status": "converted",
            "conversion_backend": "marker-pdf",
            "elapsed_seconds": 1,
            "output_path": str(worker.output_markdown),
        }

    async def send_deeplynx_artifact(**kwargs):
        captured.update(kwargs)
        assert Path(kwargs["file_path"]).read_text(encoding="utf-8") == "# Converted\n"
        return _artifact_ref(
            artifact_id="md-1",
            name=kwargs["name"],
            uri="deeplynx://items/2",
            mime_type=kwargs["mime_type"],
            metadata=kwargs["metadata"],
        )

    async def noop_status(*args, **kwargs):
        return None

    monkeypatch.setattr(_MODULE, "_try_storage_config", lambda: object())
    monkeypatch.setattr(agent, "get_artifact_file_path", get_file_path)
    monkeypatch.setattr(agent, "resolve_artifact_reference", lambda *args, **kwargs: _artifact_ref())
    monkeypatch.setattr(agent, "run_worker", run_worker)
    monkeypatch.setattr(agent, "send_deeplynx_artifact", send_deeplynx_artifact)
    monkeypatch.setattr(agent, "send_status_update", noop_status)
    monkeypatch.setattr(agent, "send_conversation_message", noop_status)

    result = asyncio.run(
        agent._execute(
            {"artifact_id": "pdf-1", "name": "converted report"},
            _context(),
            AsyncMock(spec=EventQueue),
        )
    )

    assert result["status"] == "converted"
    assert result["artifact_id"] == "md-1"
    assert captured["name"] == "converted_report.md"
    assert captured["mime_type"] == "text/markdown"
    assert captured["tags"] == ["markdown", "converted", "pdf-to-markdown"]
    assert captured["metadata"]["source_artifact_id"] == "pdf-1"
    assert captured["metadata"]["conversion_backend"] == "marker-pdf"
