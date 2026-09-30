"""Validate .env.example completeness and cross-references with docker-compose.yml."""

import pytest


REQUIRED_VARS = [
    "ANTHROPIC_API_KEY",
    "AGENT_SECRET",
    "REDIS_PASSWORD",
    "BACKEND_JWT_SECRET",
    "LITELLM_MASTER_KEY",
    "LITELLM_DB_PASSWORD",
]

LLM_CONFIG_VARS = [
    "LLM_CONFIG_PROFILE",
    "LLM_CONFIG_FILE",
    "ANTHROPIC_API_KEY",
    "EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
    "INL_HPC_API_KEY",
    "INL_LITELLM_API_KEY",
    "AMSC_I2_API_KEY",
]


def test_env_example_exists(env_example_path):
    assert env_example_path.exists(), ".env.example file does not exist"


@pytest.mark.parametrize("var", REQUIRED_VARS)
def test_required_var_present(env_example_vars, var):
    assert var in env_example_vars, f"Required variable '{var}' missing from .env.example"


@pytest.mark.parametrize("var", LLM_CONFIG_VARS)
def test_llm_config_var_present(env_example_vars, var):
    assert var in env_example_vars, f"LLM configuration variable '{var}' missing from .env.example"


def test_all_compose_var_refs_have_entry_or_default(compose_var_refs, env_example_vars):
    """Every ${VAR} in docker-compose.yml without a :-default must exist in .env.example.

    Well-known optional vars (proxy settings) are excluded since they are
    intentionally commented out in .env.example and only needed behind a corporate proxy.
    """
    optional_vars = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    missing = []
    for var_name, has_default in compose_var_refs:
        if not has_default and var_name not in env_example_vars and var_name not in optional_vars:
            missing.append(var_name)
    assert not missing, f"Compose references without defaults or .env.example entry: {missing}"


def test_no_empty_required_vars(env_example_vars):
    """Required vars should have placeholder text, not empty values."""
    for var in REQUIRED_VARS:
        if var in env_example_vars:
            assert env_example_vars[var] != "", f"Required variable '{var}' has empty value"


def test_redis_url_references_redis_password(env_example_vars):
    redis_url = env_example_vars.get("REDIS_URL", "")
    assert "${REDIS_PASSWORD}" in redis_url or "REDIS_PASSWORD" in redis_url, (
        "REDIS_URL should reference REDIS_PASSWORD for password interpolation"
    )


def test_port_vars_have_values(env_example_vars):
    """Port variables should have values defined in .env.example."""
    port_vars = ["LITELLM_LOCAL_PORT", "REDIS_LOCAL_PORT", "CLIENT_AGENT_PORT", "BACKEND_PORT"]
    missing = [v for v in port_vars if v not in env_example_vars]
    assert not missing, f"Port variables missing from .env.example: {missing}"
