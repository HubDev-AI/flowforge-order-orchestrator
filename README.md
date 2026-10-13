# flowforge-order-orchestrator


Asynchronous order orchestrator: a REST API submits orders that run through a config-ordered pipeline of stage services over RabbitMQ, with order state in Postgres. Design record: `docs/plans/`.

## Develop

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not contract and not e2e"
uv run pytest -m contract  # real Postgres via testcontainers, needs Docker
```

## Run

```sh
docker compose up -d --build --wait
```

The pipeline is `config/pipeline.toml`.
