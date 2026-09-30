"""Validate health check configurations for infrastructure services."""


def _get_healthcheck(compose_data, service_name) -> dict | None:
    return compose_data["services"][service_name].get("healthcheck")


def test_postgres_has_healthcheck(compose_data):
    hc = _get_healthcheck(compose_data, "litellm-db")
    assert hc is not None, "litellm-db should define a healthcheck"


def test_postgres_healthcheck_command(compose_data):
    hc = _get_healthcheck(compose_data, "litellm-db")
    test_cmd = hc["test"]
    # test can be a list like ["CMD-SHELL", "pg_isready -U litellm"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else test_cmd
    assert "pg_isready" in joined
    assert "-U litellm" in joined


def test_litellm_has_healthcheck(compose_data):
    hc = _get_healthcheck(compose_data, "litellm")
    assert hc is not None, "litellm should define a healthcheck"


def test_litellm_healthcheck_url(compose_data):
    hc = _get_healthcheck(compose_data, "litellm")
    test_cmd = hc["test"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else test_cmd
    assert "health/liveliness" in joined
    assert "localhost:4000" in joined


def test_litellm_healthcheck_has_start_period(compose_data):
    hc = _get_healthcheck(compose_data, "litellm")
    assert "start_period" in hc, "litellm healthcheck should have start_period (slow startup)"


def test_valkey_has_no_healthcheck(compose_data):
    """Document current state: Valkey has no healthcheck defined."""
    hc = _get_healthcheck(compose_data, "redis")
    assert hc is None, "Valkey currently has no healthcheck (update this test if one is added)"


def test_backend_has_healthcheck(compose_data):
    """The backend must have one: it carries `restart: unless-stopped`.

    Without a healthcheck the restart policy papers over a backend that boots but
    cannot serve -- the container reports "Up" either way -- which is precisely the
    class of silent failure the mesh subprocess pool was added to make visible.
    """
    hc = _get_healthcheck(compose_data, "backend")
    assert hc is not None, "backend should define a healthcheck"


def test_backend_healthcheck_probes_healthz(compose_data):
    hc = _get_healthcheck(compose_data, "backend")
    test_cmd = hc["test"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else test_cmd
    assert "/healthz" in joined
    assert "localhost:8000" in joined, "must probe the container port, not the host port"


def test_backend_healthcheck_has_start_period(compose_data):
    """The entrypoint runs `alembic upgrade head` before uvicorn, so first boot is slow."""
    hc = _get_healthcheck(compose_data, "backend")
    assert "start_period" in hc
