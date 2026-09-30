"""Config + startup guards.

Covers DEP-1 (fold ``REDIS_PASSWORD`` into a credential-less ``REDIS_URL``) and
BLK-3 (the lifespan refuses to start without a JWT signing secret).
"""

import asyncio

import pytest

from app import config as config_mod
from app.config import settings
from app.main import app, lifespan


# --------------------------------------------------------------------------- #
# DEP-1 — REDIS_PASSWORD folding
# --------------------------------------------------------------------------- #


def test_redis_url_folds_password_when_url_has_no_userinfo(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "s3cr3t")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")
    assert config_mod._redis_url({}) == "redis://:s3cr3t@redis:6379"


def test_redis_url_noop_when_url_already_has_credentials(monkeypatch):
    # The documented .env.example form embeds the password in the URL — untouched.
    monkeypatch.setenv("REDIS_PASSWORD", "s3cr3t")
    monkeypatch.setenv("REDIS_URL", "redis://:existing@redis:6379")
    assert config_mod._redis_url({}) == "redis://:existing@redis:6379"


def test_redis_url_noop_when_no_password(monkeypatch):
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")
    assert config_mod._redis_url({}) == "redis://redis:6379"


def test_redis_url_url_encodes_special_chars(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "p@ss/w:rd")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")
    assert config_mod._redis_url({}) == "redis://:p%40ss%2Fw%3Ard@redis:6379"


# --------------------------------------------------------------------------- #
# BLK-3 — lifespan requires a signing secret
# --------------------------------------------------------------------------- #


def test_lifespan_raises_without_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "", raising=False)

    async def run() -> None:
        async with lifespan(app):
            pass

    with pytest.raises(RuntimeError, match="jwt_secret is not configured"):
        asyncio.run(run())


def test_lifespan_starts_with_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "x" * 32, raising=False)

    async def run() -> bool:
        async with lifespan(app):
            return True

    assert asyncio.run(run()) is True


# --------------------------------------------------------------------------- #
# Admin-db migrations are a deploy step, NOT a lifespan step
#
# They used to run inside the lifespan, which is wrong the moment BACKEND_WORKERS > 1:
# uvicorn forks N processes, each runs the lifespan, and all N would `alembic upgrade
# head` against one database at once. The gate is now docker-entrypoint.sh, with a
# Postgres advisory lock in alembic/env.py covering two containers racing. These two
# tests are the regression guard for both halves.
# --------------------------------------------------------------------------- #


def test_lifespan_does_not_migrate(monkeypatch):
    """No migration machinery may be reachable from the lifespan.

    A future "just apply migrations at startup, it's convenient" patch reintroduces the
    multi-worker race, so this asserts on the absence of the hook rather than on
    behaviour that would be hard to observe.
    """
    from app import main as app_main

    assert not hasattr(app_main, "_run_admin_db_migrations")
    assert "alembic" not in dir(app_main)

    monkeypatch.setattr(settings, "jwt_secret", "x" * 32, raising=False)

    async def run() -> bool:
        async with lifespan(app):
            return True

    # Starts cleanly with no admin-db reachable at all -- proof it never touches one.
    assert asyncio.run(run()) is True


def test_entrypoint_migrates_before_exec_and_fails_closed():
    """``docker-entrypoint.sh`` must migrate, gate on the result, then exec uvicorn."""
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"
    body = script.read_text()

    assert "set -e" in body, "a failed migration has to stop the boot"
    migrate = body.index("alembic upgrade head")
    serve = body.index("exec uvicorn")
    assert migrate < serve, "migrations must complete before the server starts"
    assert "BACKEND_WORKERS" in body


def test_alembic_takes_an_advisory_lock_before_running_migrations():
    """Two containers (or a developer CLI) can still race the entrypoint, so
    ``env.py`` serialises on a Postgres advisory lock -- taken *before*
    ``run_migrations`` so the blocked caller re-reads an updated ``alembic_version``."""
    from pathlib import Path

    body = (Path(__file__).resolve().parent.parent / "alembic" / "env.py").read_text()
    # Scoped to do_run_migrations: the offline path also calls run_migrations, and it
    # has no connection to take a lock on.
    online = body[body.index("def do_run_migrations("):]
    lock = online.index("pg_advisory_xact_lock")
    run = online.index("context.run_migrations()")
    assert lock < run


# --------------------------------------------------------------------------- #
# _cookie_secure: the `Secure` flag must follow the browser-facing scheme, not
# `debug`. Getting this wrong drops the session cookie silently in Safari over
# http:// and on any non-localhost http origin (see app/config._cookie_secure).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "public_url,expected",
    [
        ("https://prometheus.example.gov", True),
        ("HTTPS://Prometheus.Example.Gov", True),  # scheme comparison is case-insensitive
        ("http://localhost:5150", False),
        ("http://10.0.0.7:5150", False),
    ],
)
def test_cookie_secure_follows_the_public_url_scheme(monkeypatch, public_url, expected):
    from app.config import _cookie_secure

    monkeypatch.setenv("BACKEND_PUBLIC_URL", public_url)
    monkeypatch.delenv("BACKEND_COOKIE_SECURE", raising=False)
    # `debug` is deliberately the value that would give the opposite answer.
    assert _cookie_secure({"debug": not expected}) is expected


def test_cookie_secure_falls_back_to_debug_without_a_public_url(monkeypatch):
    from app.config import _cookie_secure

    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BACKEND_COOKIE_SECURE", raising=False)
    assert _cookie_secure({"debug": False}) is True
    assert _cookie_secure({"debug": True}) is False
    assert _cookie_secure({}) is True


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("on", True), ("0", False), ("no", False)])
def test_cookie_secure_explicit_override_wins(monkeypatch, raw, expected):
    from app.config import _cookie_secure

    # Needed when TLS is terminated by an ingress that doesn't set BACKEND_PUBLIC_URL.
    monkeypatch.setenv("BACKEND_PUBLIC_URL", "http://localhost:5150")
    monkeypatch.setenv("BACKEND_COOKIE_SECURE", raw)
    assert _cookie_secure({"debug": True}) is expected


def test_cookie_secure_ignores_an_empty_override(monkeypatch):
    from app.config import _cookie_secure

    monkeypatch.setenv("BACKEND_PUBLIC_URL", "https://prometheus.example.gov")
    monkeypatch.setenv("BACKEND_COOKIE_SECURE", "   ")
    assert _cookie_secure({"debug": True}) is True
