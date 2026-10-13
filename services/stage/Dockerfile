FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.14.8-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/bus/pyproject.toml packages/bus/
COPY packages/stage_kit/pyproject.toml packages/stage_kit/
COPY services/orchestrator/pyproject.toml services/orchestrator/
COPY services/stage/pyproject.toml services/stage/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace --no-dev --package flowforge-stage

COPY packages/contracts packages/contracts
COPY packages/bus packages/bus
COPY packages/stage_kit packages/stage_kit
COPY services/stage services/stage
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package flowforge-stage

CMD ["python", "services/stage/main.py"]
