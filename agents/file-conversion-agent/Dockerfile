# syntax=docker/dockerfile:1
ARG PROMETHEUS_API_BASE_IMAGE_HASH
FROM containers.github.inl.gov/genesis/prometheus_api:${PROMETHEUS_API_BASE_IMAGE_HASH}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT="/usr/local" \
    UV_LINK_MODE=copy

COPY pyproject.toml /app/
RUN --mount=type=cache,id=prometheus-uv-cache,target=/root/.cache/uv,sharing=locked \
    uv sync --no-dev --no-install-project --inexact

COPY agent.py /app/agent.py
COPY pdf_to_markdown_smoke.py /app/pdf_to_markdown_smoke.py
COPY agent_card.json /app/agent_card.json
RUN chmod +x /app/pdf_to_markdown_smoke.py

EXPOSE 8000

CMD ["python", "agent.py"]
