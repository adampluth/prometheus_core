"""Offline tests for artifact classification + the content and preview routes (task 07).

The classifier tests exercise ``classify_render_hint`` across json_type / tags /
mime / extension precedence. The router tests drive
``GET /api/conversations/{cid}/artifacts/{aid}/content`` against a ``TestClient`` with
a fakeredis-seeded conversation catalog and ``uri_resolver`` stubbed — no live agent,
DeepLynx, or network. The auth/200 cases also prove the router is mounted in
``app/main.py``.
"""

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import uri_resolver
from app.conversation_state import ConversationStateClient
from app.routers import artifacts as artifacts_router
from app.services.previews import PreviewFailed, PreviewUnavailable, PreviewUnreadable
from app.services.artifacts import (
    artifact_download_payload,
    artifact_identity,
    classify_render_hint,
    to_artifact_meta,
)


# --------------------------------------------------------------------------- #
# classify_render_hint — precedence: json_type → tags → mime → extension
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "artifact,expected",
    [
        ({"json_type": "prometheus.thermal_analysis.result.v1"}, "thermal"),
        ({"tags": ["ifc"]}, "ifc"),
        ({"tags": ["exodus"]}, "mesh"),
        ({"tags": ["stl"]}, "mesh"),
        ({"tags": ["citation"]}, "reference"),
        ({"tags": ["data"]}, "json"),  # tag alias maps to the json viewer kind
        ({"mime_type": "model/ifc"}, "ifc"),
        ({"mime_type": "application/exodus"}, "mesh"),
        ({"mime_type": "application/x-netcdf"}, "mesh"),  # T-6: exodus container MIME
        ({"mime_type": "application/x-hdf5"}, "mesh"),  # T-6: exodus container MIME
        ({"mime_type": "model/stl"}, "mesh"),
        # A-2: a specific MIME outranks a generic tag…
        ({"tags": ["data"], "mime_type": "application/pdf"}, "pdf"),
        # …but a specific tag outranks a *generic* download MIME.
        ({"tags": ["exodus"], "mime_type": "application/octet-stream"}, "mesh"),
        ({"mime_type": "text/x-mermaid"}, "mermaid"),
        ({"mime_type": "application/pdf"}, "pdf"),
        ({"mime_type": "application/json"}, "json"),
        ({"mime_type": "text/csv"}, "file"),  # A-9: CSV downloads, not JSONViewer
        ({"mime_type": "image/png"}, "image"),
        ({"mime_type": "image/svg+xml"}, "image"),  # SVG folds into image (React)
        ({"mime_type": "application/octet-stream"}, "file"),
        ({"mime_type": "text/markdown"}, "report"),  # A-1: rendered inline, not downloaded
        ({"mime_type": "text/plain"}, "report"),  # A-1: old app rendered plain text inline too
        ({"mime_type": "model/step"}, "file"),  # CAD → downloadable file
        ({"mime_type": "application/x-ipynb+json"}, "file"),  # notebook → downloadable file
        ({"name": "analysis.ipynb"}, "file"),  # notebook extension fallback
        ({"uri": "deeplynx://x/items/1", "name": "model.ifc"}, "ifc"),  # extension fallback
        ({"name": "report.pdf"}, "pdf"),
        ({"name": "notes.md"}, "report"),  # A-1: markdown extension fallback
        ({"name": "mesh.exo"}, "mesh"),
        ({"json_body": {"k": "v"}}, "json"),  # inline JSON, no other signal
        ({"mime_type": "application/x-whatever"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_classify_render_hint(artifact, expected):
    assert classify_render_hint(artifact) == expected


def test_json_type_beats_mime():
    # A thermal result mislabeled with a generic MIME still routes to the viewer.
    artifact = {"json_type": "prometheus.thermal_analysis.result.v1", "mime_type": "application/json"}
    assert classify_render_hint(artifact) == "thermal"


def test_mime_normalized_with_charset_param():
    assert classify_render_hint({"mime_type": "application/pdf; charset=binary"}) == "pdf"


def test_to_artifact_meta_carries_hint_and_skips_conversation_message():
    meta = to_artifact_meta({"artifact_id": "a1", "name": "r.pdf", "mime_type": "application/pdf"}, "c1")
    assert meta is not None and meta.render_hint == "pdf"

    assert to_artifact_meta({"name": "conversation_message"}, "c1") is None
    assert to_artifact_meta({"metadata": {"kind": "conversation_message"}}, "c1") is None


def test_to_artifact_meta_scopes_content_url_to_conversation():
    meta = to_artifact_meta({"artifact_id": "a/1", "name": "x.png"}, "c 1")
    assert meta is not None
    assert meta.content_url == "/api/conversations/c%201/artifacts/a%2F1/content"


def test_to_artifact_meta_preview_url_only_when_a_previewer_applies():
    mesh = to_artifact_meta({"artifact_id": "m1", "name": "t.exo", "uri": "file:///t.exo"}, "c1")
    assert mesh is not None and mesh.preview_url == "/api/conversations/c1/artifacts/m1/preview"
    pdf = to_artifact_meta({"artifact_id": "p1", "name": "r.pdf", "uri": "file:///r.pdf"}, "c1")
    assert pdf is not None and pdf.preview_url is None
    sourceless = to_artifact_meta({"artifact_id": "m2", "name": "t.exo"}, "c1")  # nothing to build from
    assert sourceless is not None and sourceless.preview_url is None


def test_to_artifact_meta_populates_citation_for_reference(monkeypatch):
    # CON-1: reference artifacts carry source/section/page from metadata onto the wire.
    meta = to_artifact_meta(
        {
            "artifact_id": "ref1",
            "tags": ["reference"],
            "metadata": {"source": "ASME BPVC", "section": "II-D", "page": 42},
        },
        "c1",
    )
    assert meta is not None and meta.render_hint == "reference"
    assert meta.citation is not None
    assert meta.citation.source == "ASME BPVC"
    assert meta.citation.section == "II-D"
    assert meta.citation.page == "42"  # coerced to str


def test_to_artifact_meta_citation_absent_when_no_metadata():
    meta = to_artifact_meta({"artifact_id": "a1", "mime_type": "application/pdf"}, "c1")
    assert meta is not None and meta.citation is None


# --------------------------------------------------------------------------- #
# artifact_download_payload — inline serialization
# --------------------------------------------------------------------------- #


def test_download_payload_json_body():
    content, ext, mime = artifact_download_payload({"json_body": {"a": 1}})
    assert ext == ".json" and mime == "application/json"
    assert b'"a": 1' in content


def test_download_payload_text_prefers_markdown_extension():
    content, ext, mime = artifact_download_payload({"text": "# hi"}, mime_type="text/markdown")
    assert ext == ".md" and mime == "text/markdown" and content == b"# hi"


def test_download_payload_none_when_nothing_inline():
    assert artifact_download_payload({"name": "x", "uri": "file:///tmp/x"}) is None


# --------------------------------------------------------------------------- #
# GET /api/conversations/{cid}/artifacts/{aid}/content
# --------------------------------------------------------------------------- #


def _authed_client(monkeypatch, artifacts=None, *, owner: str = "alice") -> TestClient:
    """TestClient with a valid cookie and a Redis catalog for conversation ``c1``.

    The catalog is seeded the way ``ArtifactRegistry`` writes it — one hash field per
    ``artifact_id`` — except id-less artifacts, which land under an unrelated field
    so the lookup has to fall back to matching their synthetic identity.
    """
    from app import security
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "jwt_secret", "test-secret", raising=False)
    monkeypatch.setattr(security.settings, "jwt_secret", "test-secret", raising=False)

    fake = fakeredis.FakeRedis(decode_responses=True)
    fake.sadd(f"user:convs:{owner}", "c1")
    for i, artifact in enumerate(artifacts or []):
        field = artifact.get("artifact_id") or f"legacy-{i}"
        fake.hset("artifacts:conv:c1:by_id", field, json.dumps(artifact))
    state = ConversationStateClient("redis://unused")
    state._client = fake
    monkeypatch.setattr(artifacts_router, "_client", lambda: state)

    # conftest installs a fresh session store, so the session is unbound: content
    # must not depend on it.
    token = security.issue_token(security.User(identifier="alice"), sid="sid-artifact")
    client = TestClient(app)
    client.cookies.set(settings.cookie_name, token)
    return client


def _url(artifact_id: str, conversation_id: str = "c1") -> str:
    return f"/api/conversations/{conversation_id}/artifacts/{artifact_id}/content"


def test_content_requires_auth():
    from app.main import app

    client = TestClient(app)
    resp = client.get(_url("a1"))
    assert resp.status_code == 401


def test_content_404_when_not_in_catalog(monkeypatch):
    client = _authed_client(monkeypatch, artifacts=[{"artifact_id": "other"}])
    resp = client.get(_url("missing"))
    assert resp.status_code == 404


def test_content_streams_inline_json_body(monkeypatch):
    client = _authed_client(
        monkeypatch,
        artifacts=[{"artifact_id": "a1", "name": "result", "json_body": {"answer": 42}}],
    )
    resp = client.get(_url("a1"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"answer": 42}
    assert 'filename="result.json"' in resp.headers["content-disposition"]


def test_content_streams_file_uri(monkeypatch, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    client = _authed_client(
        monkeypatch,
        artifacts=[{"artifact_id": "a1", "name": "doc.pdf", "mime_type": "application/pdf", "uri": f"file://{f}"}],
    )
    resp = client.get(_url("a1"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.4 fake"
    assert "immutable" in resp.headers["cache-control"]


def test_content_404_for_conversation_owned_by_someone_else(monkeypatch):
    client = _authed_client(monkeypatch, artifacts=[{"artifact_id": "a1", "json_body": {}}], owner="bob")
    assert client.get(_url("a1")).status_code == 404


async def test_content_allowed_for_session_bound_conversation_without_ownership(monkeypatch, session_store):
    # No identity keys → the client-agent never records ownership; the live chat's
    # own (server-minted) conversation must still serve its artifacts.
    client = _authed_client(monkeypatch, artifacts=[{"artifact_id": "a1", "json_body": {"k": 1}}], owner="nobody")
    await session_store.reset("sid-artifact", "c1")
    assert client.get(_url("a1")).json() == {"k": 1}


def test_content_404_for_other_conversation(monkeypatch):
    client = _authed_client(monkeypatch, artifacts=[{"artifact_id": "a1", "json_body": {}}])
    assert client.get(_url("a1", conversation_id="c2")).status_code == 404


def test_content_502_when_resolve_fails(monkeypatch):
    async def _fail(uri, *, cache_dir, artifact=None, artifact_id=None):
        return None

    monkeypatch.setattr(uri_resolver, "resolve_to_local_path", _fail)
    client = _authed_client(
        monkeypatch,
        artifacts=[{"artifact_id": "a1", "name": "x", "uri": "deeplynx://org/items/1"}],
    )
    resp = client.get(_url("a1"))
    assert resp.status_code == 502


def test_content_404_when_in_catalog_but_no_content(monkeypatch):
    client = _authed_client(monkeypatch, artifacts=[{"artifact_id": "a1", "name": "empty"}])
    resp = client.get(_url("a1"))
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/conversations/{cid}/artifacts/{aid}/preview
# --------------------------------------------------------------------------- #

_MESH_ART = {"artifact_id": "m1", "name": "t.exo", "mime_type": "application/x-exodus", "uri": "file:///t.exo"}


def _stub_preview_props(monkeypatch, result):
    calls = []

    async def _props(previewer, artifact):
        calls.append((previewer.name, artifact))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(artifacts_router, "preview_props", _props)
    return calls


def test_preview_route_returns_viewer_props(monkeypatch):
    calls = _stub_preview_props(monkeypatch, {"manifest": {}, "baseUrl": "/api/previews/k1"})
    client = _authed_client(monkeypatch, artifacts=[_MESH_ART])
    resp = client.get("/api/conversations/c1/artifacts/m1/preview")
    assert resp.status_code == 200
    assert resp.json()["baseUrl"] == "/api/previews/k1"
    assert "immutable" in resp.headers["cache-control"]
    assert calls[0][0] == "mesh"  # the registry picked the mesh previewer…
    assert calls[0][1]["uri"] == "file:///t.exo"  # …and handed it the catalog entry


def test_preview_route_404_without_a_previewer(monkeypatch):
    calls = _stub_preview_props(monkeypatch, {})
    client = _authed_client(monkeypatch, artifacts=[{"artifact_id": "p1", "name": "r.pdf", "uri": "file:///r.pdf"}])
    assert client.get("/api/conversations/c1/artifacts/p1/preview").status_code == 404
    assert not calls


def test_preview_route_404_for_conversation_owned_by_someone_else(monkeypatch):
    calls = _stub_preview_props(monkeypatch, {})
    client = _authed_client(monkeypatch, artifacts=[_MESH_ART], owner="bob")
    assert client.get("/api/conversations/c1/artifacts/m1/preview").status_code == 404
    assert not calls


@pytest.mark.parametrize(
    "error,status",
    [
        (PreviewUnavailable("gone"), 502),
        (PreviewUnreadable("not exodus"), 422),
        (PreviewFailed("the preview builder crashed"), 500),
        (RuntimeError("boom"), 500),
    ],
)
def test_preview_route_maps_failures(monkeypatch, error, status):
    _stub_preview_props(monkeypatch, error)
    client = _authed_client(monkeypatch, artifacts=[_MESH_ART])
    assert client.get("/api/conversations/c1/artifacts/m1/preview").status_code == status


def test_preview_route_shows_pool_failures_but_hides_unexpected_ones(monkeypatch):
    # PreviewFailed messages are written for the user; an arbitrary exception's are not.
    client = _authed_client(monkeypatch, artifacts=[_MESH_ART])
    _stub_preview_props(monkeypatch, PreviewFailed("took longer than 300s"))
    assert "took longer than 300s" in client.get("/api/conversations/c1/artifacts/m1/preview").json()["detail"]
    _stub_preview_props(monkeypatch, RuntimeError("/secret/path"))
    assert "/secret/path" not in client.get("/api/conversations/c1/artifacts/m1/preview").json()["detail"]


# --------------------------------------------------------------------------- #
# A-4 — stable identity for id-less artifacts
# --------------------------------------------------------------------------- #


def test_artifact_identity_is_stable_distinct_and_prefers_explicit():
    a = {"name": "one.txt", "uri": "deeplynx://x/items/1"}
    b = {"name": "two.txt", "uri": "deeplynx://x/items/2"}
    ida, idb = artifact_identity(a), artifact_identity(b)
    assert ida and idb and ida != idb  # non-empty and distinct (no "" collision)
    assert ida == artifact_identity(dict(a))  # deterministic
    assert artifact_identity({"artifact_id": "explicit", "name": "x"}) == "explicit"


def test_content_fetches_idless_inline_artifact(monkeypatch):
    art = {"name": "notes", "json_body": {"k": "v"}}  # no artifact_id/id
    client = _authed_client(monkeypatch, artifacts=[art])
    resp = client.get(_url(artifact_identity(art)))
    assert resp.status_code == 200
    assert resp.json() == {"k": "v"}


# --------------------------------------------------------------------------- #
# A-5 / A-6 — Content-Disposition type + filename escaping
# --------------------------------------------------------------------------- #


def test_content_disposition_is_attachment_for_download(monkeypatch, tmp_path):
    f = tmp_path / "logs.zip"
    f.write_bytes(b"PK\x03\x04zip")
    client = _authed_client(
        monkeypatch,
        artifacts=[{"artifact_id": "a1", "name": "logs.zip", "mime_type": "application/zip", "uri": f"file://{f}"}],
    )
    resp = client.get(_url("a1"))
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment;")


def test_content_disposition_escapes_dangerous_filename(monkeypatch):
    art = {"artifact_id": "a1", "name": 'ev"il\nname', "json_body": {"k": 1}}
    client = _authed_client(monkeypatch, artifacts=[art])
    cd = client.get(_url("a1")).headers["content-disposition"]
    assert "\n" not in cd and "\r" not in cd  # no header injection
    # quote + newline stripped from the ascii name (.json appended for the inline body)
    assert 'filename="evilname.json"' in cd
    assert "filename*=UTF-8''" in cd  # RFC 5987 form carries the exact name
