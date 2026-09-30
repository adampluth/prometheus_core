"""Validate volume mounts and filesystem paths."""


def _get_volumes(svc: dict) -> list[str]:
    """Get volume strings from a service definition."""
    return [str(v) for v in svc.get("volumes", [])]


def test_litellm_config_bind_mount(compose_data):
    volumes = _get_volumes(compose_data["services"]["litellm"])
    assert any("litellm_config.yaml:/app/config.yaml" in v for v in volumes), (
        "litellm service should bind-mount litellm_config.yaml"
    )


def test_litellm_config_file_exists(project_root):
    assert (project_root / "litellm_config.yaml").exists()


# Only these two host paths may be bind-mounted at runtime. Every other service
# must run purely from its image, so the stack stays deployable to plain Docker or
# Kubernetes without provisioning a host path or an RWX volume for source. The
# frontend entry is the Vite dev server, which needs host sources for HMR and is
# gated behind the "dev" profile; litellm_config.yaml is config, i.e. a ConfigMap.
ALLOWED_HOST_MOUNTS = {
    "litellm": {"./litellm_config.yaml:/app/config.yaml"},
    "frontend-dev": {"./frontend:/app"},
}


def _host_mounts(svc: dict) -> set[str]:
    """Bind mounts of a host path: 'source:target' whose source is a path.

    A single-segment entry such as '/app/node_modules' is an anonymous volume, not a
    bind, and a 'name:/target' entry is a named volume — neither ties the deployment
    to the host filesystem, so neither counts here.
    """
    return {
        v for v in _get_volumes(svc)
        if ":" in v and v.split(":", 1)[0].startswith((".", "/"))
    }


def test_no_unexpected_host_bind_mounts(compose_data):
    for name, svc in compose_data["services"].items():
        assert _host_mounts(svc) <= ALLOWED_HOST_MOUNTS.get(name, set()), (
            f"service '{name}' bind-mounts a host path that is not in "
            f"ALLOWED_HOST_MOUNTS; prefer baking it into the image"
        )


def test_no_host_bind_mounts_in_agent_compose_files(project_root):
    """Same rule for the per-agent files make generate includes — no agent needs one."""
    import yaml

    paths = sorted(project_root.glob("agents/*/docker-compose.yml")) + sorted(
        project_root.glob("agents/debug-agents/*/docker-compose.yml")
    )
    assert paths, "no per-agent compose files found"
    for path in paths:
        data = yaml.safe_load(path.read_text()) or {}
        for name, svc in (data.get("services") or {}).items():
            assert not _host_mounts(svc), (
                f"{path.relative_to(project_root)}: service '{name}' bind-mounts a host path"
            )


def test_named_volumes_declared(compose_data):
    volumes = compose_data.get("volumes", {})
    assert "litellm-db-data" in volumes
    assert "valkey-data" in volumes
    assert "preview-cache" in volumes


def test_preview_cache_volume(compose_data):
    """Built artifact previews (e.g. VTP timesteps) must survive a backend restart.

    Previews are keyed by source, so a container-local /tmp meant every restart re-ran
    the (expensive, VTK-heavy) Exodus conversion for artifacts it had already built.
    PROMETHEUS_PREVIEW_CACHE_DIR has to agree with the mount point.
    """
    backend = compose_data["services"]["backend"]
    volumes = _get_volumes(backend)
    assert any("preview-cache:/var/cache/prometheus/previews" in v for v in volumes)
    assert backend["environment"]["PROMETHEUS_PREVIEW_CACHE_DIR"] == "/var/cache/prometheus/previews"


def test_postgres_data_volume(compose_data):
    volumes = _get_volumes(compose_data["services"]["litellm-db"])
    assert any("litellm-db-data:/var/lib/postgresql/data" in v for v in volumes)


def test_valkey_data_volume(compose_data):
    volumes = _get_volumes(compose_data["services"]["redis"])
    assert any("valkey-data:/data" in v for v in volumes)
