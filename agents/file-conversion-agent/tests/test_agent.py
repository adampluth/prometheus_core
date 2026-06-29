from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events import EventQueue


AGENT_DIR = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("file_conversion_agent_module", AGENT_DIR / "agent.py")
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
sys.modules.setdefault("agent", _MODULE)
_SMOKE_SPEC = importlib.util.spec_from_file_location(
    "pdf_to_markdown_smoke_module", AGENT_DIR / "pdf_to_markdown_smoke.py"
)
assert _SMOKE_SPEC and _SMOKE_SPEC.loader
_SMOKE_MODULE = importlib.util.module_from_spec(_SMOKE_SPEC)
_SMOKE_SPEC.loader.exec_module(_SMOKE_MODULE)

FileConversionAgent = _MODULE.FileConversionAgent
PdfToMarkdownWorker = _MODULE.PdfToMarkdownWorker


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
        assert worker.fast_mode is False
        worker.output_markdown.write_text("# Converted\n", encoding="utf-8")
        return {
            "status": "converted",
            "conversion_backend": "opendataloader-pdf",
            "conversion_backend_mode": "hybrid",
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
    assert captured["metadata"]["conversion_backend"] == "opendataloader-pdf"
    assert captured["metadata"]["conversion_backend_mode"] == "hybrid"
    assert result["conversion_backend"] == "opendataloader-pdf"
    assert result["conversion_backend_mode"] == "hybrid"


def test_pdf_to_markdown_fast_mode_uploads_fast_metadata(monkeypatch, tmp_path: Path) -> None:
    agent = FileConversionAgent(agent_name="File Conversion Agent")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    captured = {}

    async def get_file_path(*args, **kwargs):
        return source

    async def run_worker(worker, context, event_queue, *, poll_interval):
        assert worker.fast_mode is True
        worker.output_markdown.write_text("# Converted\n", encoding="utf-8")
        return {
            "status": "converted",
            "conversion_backend": "opendataloader-pdf",
            "conversion_backend_mode": "fast",
            "elapsed_seconds": 1,
            "output_path": str(worker.output_markdown),
        }

    async def send_deeplynx_artifact(**kwargs):
        captured.update(kwargs)
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
            {"artifact_id": "pdf-1", "fast_mode": True},
            _context(),
            AsyncMock(spec=EventQueue),
        )
    )

    assert result["status"] == "converted"
    assert captured["metadata"]["conversion_backend_mode"] == "fast"
    assert result["conversion_backend_mode"] == "fast"


def test_pdf_to_markdown_rejects_invalid_fast_mode(monkeypatch) -> None:
    agent = FileConversionAgent(agent_name="File Conversion Agent")
    monkeypatch.setattr(_MODULE, "_try_storage_config", lambda: object())

    result = asyncio.run(
        agent._execute(
            {"artifact_id": "pdf-1", "fast_mode": "sometimes"},
            _context(),
            AsyncMock(spec=EventQueue),
        )
    )

    assert result["status"] == "error"
    assert result["message"] == "Parameter 'fast_mode' must be a boolean."


def test_pdf_to_markdown_worker_invokes_opendataloader_hybrid_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output = tmp_path / "converted.md"
    commands = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            commands.append(command)
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "source.md").write_text("# Converted\n", encoding="utf-8")

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", ""

    monkeypatch.setattr(_MODULE.subprocess, "Popen", FakeProcess)

    worker = PdfToMarkdownWorker(source_pdf=source, output_markdown=output)
    monkeypatch.setattr(worker, "emit_status_update", lambda *args, **kwargs: None)

    result = worker.run()

    command = commands[0]
    assert command[0] == "opendataloader-pdf"
    assert command[1] == str(source)
    assert command[command.index("--format") + 1] == "markdown"
    assert command[command.index("--image-output") + 1] == "off"
    assert command[command.index("--hybrid") + 1] == "docling-fast"
    assert command[command.index("--hybrid-mode") + 1] == "full"
    assert command[command.index("--hybrid-url") + 1] == "http://file-conversion-hybrid:5002"
    assert command[command.index("--hybrid-timeout") + 1] == "0"
    assert output.read_text(encoding="utf-8") == "# Converted\n"
    assert result["status"] == "converted"
    assert result["conversion_backend"] == "opendataloader-pdf"
    assert result["conversion_backend_mode"] == "hybrid"


def test_pdf_to_markdown_worker_fast_mode_skips_hybrid(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output = tmp_path / "converted.md"
    commands = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            commands.append(command)
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "source.md").write_text("# Converted\n", encoding="utf-8")

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", ""

    monkeypatch.setattr(_MODULE.subprocess, "Popen", FakeProcess)

    worker = PdfToMarkdownWorker(source_pdf=source, output_markdown=output, fast_mode=True)
    monkeypatch.setattr(worker, "emit_status_update", lambda *args, **kwargs: None)

    result = worker.run()

    command = commands[0]
    assert "--hybrid" not in command
    assert output.read_text(encoding="utf-8") == "# Converted\n"
    assert result["conversion_backend_mode"] == "fast"


def test_pdf_to_markdown_worker_reports_opendataloader_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output = tmp_path / "converted.md"

    class FakeProcess:
        returncode = 1

        def __init__(self, command, **kwargs):
            pass

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", "java failed"

    monkeypatch.setattr(_MODULE.subprocess, "Popen", FakeProcess)

    worker = PdfToMarkdownWorker(source_pdf=source, output_markdown=output)
    monkeypatch.setattr(worker, "emit_status_update", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="opendataloader-pdf failed: java failed"):
        worker.run()


def test_pdf_to_markdown_smoke_script_invokes_worker(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output = tmp_path / "source.md"
    captured = {}

    class FakeWorker:
        def __init__(self, *, source_pdf, output_markdown, timeout_seconds, fast_mode):
            captured["source_pdf"] = source_pdf
            captured["output_markdown"] = output_markdown
            captured["timeout_seconds"] = timeout_seconds
            captured["fast_mode"] = fast_mode

        def run(self):
            output.write_text("# Converted\n", encoding="utf-8")
            return {
                "status": "converted",
                "conversion_backend": "opendataloader-pdf",
                "conversion_backend_mode": "hybrid",
                "output_path": str(output),
            }

    monkeypatch.setattr(_SMOKE_MODULE, "LocalPdfToMarkdownWorker", FakeWorker)

    status = _SMOKE_MODULE.main([str(source), str(output), "--timeout-seconds", "7"])

    out = capsys.readouterr().out
    assert status == 0
    assert '"status": "converted"' in out
    assert captured["source_pdf"] == source
    assert captured["output_markdown"] == output
    assert captured["timeout_seconds"] == 7
    assert captured["fast_mode"] is False
    assert output.read_text(encoding="utf-8") == "# Converted\n"
