#!/bin/sh
# Backend container entrypoint: migrate once, then serve.
#
# `alembic upgrade head` used to run inside app/main.py's lifespan, which is wrong the
# moment BACKEND_WORKERS > 1: uvicorn's --workers forks N processes, each importing the
# app and running the lifespan, so N processes migrate the same database at once. Here
# it runs exactly once per container, before any worker exists. (alembic/env.py also
# takes a Postgres advisory lock, which covers two *containers* starting together.)
#
# `set -e` is what makes this a gate: a failed migration must stop the boot rather than
# serve traffic against a stale or half-built schema.
set -e

echo "backend: applying admin-db migrations (alembic upgrade head)"
alembic upgrade head

# Default 1 worker. Multi-worker is valid now (session state, the SSE event bus and
# session revocation all live in Valkey), but raising it is a deliberate operational
# decision -- note that PROMETHEUS_PREVIEW_WORKERS is *per* web worker, so the peak
# preview-build child count is the product of the two.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${BACKEND_WORKERS:-1}"
