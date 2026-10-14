FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.14.8-slim AS workspace
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/stage_kit/pyproject.toml packages/stage_kit/
COPY services/orchestrator/pyproject.toml services/orchestrator/
COPY services/stage/pyproject.toml services/stage/

FROM workspace AS orchestrator
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace --no-dev --package flowforge-orchestrator
COPY packages/contracts packages/contracts
COPY services/orchestrator services/orchestrator
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package flowforge-orchestrator
USER app
WORKDIR /app/services/orchestrator
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn orchestrator.app.main:app --host 0.0.0.0 --port 8000"]

FROM workspace AS stage
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace --no-dev --package flowforge-stage
COPY packages/contracts packages/contracts
COPY packages/stage_kit packages/stage_kit
COPY services/stage services/stage
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package flowforge-stage
USER app
ENV PYTHONPATH=/app/services/stage
WORKDIR /app/services/stage
CMD ["sh", "-c", "exec celery -A worker worker --queues stage.${STAGE_NAME} --hostname ${STAGE_NAME}@%h --concurrency 1 --pool prefork --without-mingle --without-gossip --loglevel INFO"]
