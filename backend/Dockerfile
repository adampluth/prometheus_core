# syntax=docker/dockerfile:1
# Standalone FastAPI backend. Built FROM the prometheus_api base image (same
# as the agents) because the app imports prometheus_api.{a2a_stream,contract,
# storage_client} at runtime, and that package is preinstalled in the base image.
#
# `prometheus-api` is a dev-only dependency in pyproject.toml, pinned to a private
# git branch via [tool.uv.sources] for local tests. At image build time we must NOT
# fetch that private repo, so:
#   --no-dev       exclude the dev group (we don't reinstall prometheus-api)
#   --no-sources   ignore [tool.uv.sources] entirely, so uv never clones the git
#                  repo while resolving the lock (the deciding flag — --no-dev alone
#                  still resolves the source)
#   --inexact      keep the prometheus-api + deps already preinstalled in the image
#   --no-install-project  don't install backend itself; the code is COPYed below
ARG PROMETHEUS_API_BASE_IMAGE_HASH
FROM containers.github.inl.gov/genesis/prometheus_api:${PROMETHEUS_API_BASE_IMAGE_HASH}

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT="/usr/local" \
    UV_LINK_MODE=copy

# VTK (added in pyproject.toml for the Exodus/STL→VTP mesh pipeline) links libGL and
# a few X client libs at `import vtk`, even though the pipeline only uses headless
# IO/filters (no rendering context). The prometheus_api base is a Debian-based Python
# image, so install them via apt. If a mesh render 500s with
# "libGL.so.1: cannot open shared object file", this layer is the fix.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libxrender1 libxext6 libsm6 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN --mount=type=cache,id=prometheus-uv-cache,target=/root/.cache/uv,sharing=locked \
    uv sync --no-dev --no-install-project --inexact --no-sources

# Brings app/config.yml along with the package sources.
COPY app /app/app

# Alembic migrations for the admin plane (users, OIDC providers, signing keys) --
# applied by docker-entrypoint.sh before uvicorn starts, NOT from the app's lifespan
# (which would migrate once per worker; see the entrypoint for why).
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
