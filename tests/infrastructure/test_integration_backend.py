"""Integration tests for the FastAPI backend container."""

import subprocess

import httpx
import pytest


pytestmark = pytest.mark.integration


@pytest.fixture
def backend_base_url(docker_compose_up, env_test_file):
    """Return the base URL for the backend service."""
    env_vars = {}
    for line in env_test_file.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env_vars[k] = v
    port = env_vars.get("BACKEND_PORT", "18600")
    return f"http://localhost:{port}"


def test_backend_container_running(docker_compose_up):
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", "backend"],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "running"


def test_backend_healthz(backend_base_url):
    """The backend should serve its health endpoint."""
    resp = httpx.get(f"{backend_base_url}/healthz", timeout=15, follow_redirects=True)
    assert resp.status_code == 200

