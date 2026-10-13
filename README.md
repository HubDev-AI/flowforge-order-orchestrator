# flowforge-order-orchestrator

An order is submitted through a REST API and runs through a configurable sequence of slow, sometimes failing stages (inventory, fraud, shipping) until it lands in a terminal state. Anyone can ask where an order is and how it got there. The stages are simulated; the orchestration, the state model, and the visibility of the workflow are the point.

An orchestrator service owns order state in Postgres and drives the pipeline over RabbitMQ. Each stage is its own container built from one stage kit. Failures are retried with backoff; a stage that fails for good triggers the undo of every stage already dispatched, in reverse. The design record, with the alternatives that were rejected, is in [`docs/design.md`](docs/design.md).

## Run it

```sh
docker compose up -d --build --wait
```

Open <http://localhost:8000> to submit an order and watch its timeline. If port 8000 is taken, set `ORCHESTRATOR_PORT` first.

The API:

```sh
curl -s -X POST localhost:8000/api/orders \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"payload": {"sku": "A-1"}}'
# {"id": "…", "state": "pending"}   (201; the same key and payload again returns 200)

curl -s localhost:8000/api/orders/<id>    # state, current stage, and every attempt so far
```

Add an outcome script to force what the stages do, per stage, kind, and attempt (`succeed`, `retry`, `reject`, `timeout`; an attempt the script leaves unlisted succeeds):

```json
{"payload": {"sku": "A-1"},
 "script": {"fraud": {"execute": ["retry", "retry", "succeed"]},
            "inventory": {"undo": ["retry"]}}}
```

An order ends in `completed`, `failed` (every undo succeeded), or `compensation_failed` (an undo gave up).

## Change the pipeline

The stage order, retry settings, sweep interval, and adapters are in [`config/pipeline.toml`](config/pipeline.toml). Per-stage overrides go under `[overrides.<stage>]`. An order keeps the stage list it was submitted with, so a config change affects only later orders.

To add a stage:

1. Add `services/stage/handlers/<name>.py` with a `Handler` class that implements `execute` (and `undo` if the stage has side effects to reverse).
2. Add a Compose service that runs the stage image with `STAGE_NAME: <name>` (copy `stage-fraud`).
3. Add `<name>` to `stages` in `config/pipeline.toml`.

The orchestrator and the stage kit need no change.

## Develop

Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not contract and not e2e"   # unit tests, no Docker
uv run pytest -m contract                     # adapters against real Postgres and RabbitMQ
uv run pytest -m e2e                          # builds and drives the whole Compose stack
```

The contract and end-to-end suites need Docker. The end-to-end suite runs its own Compose project, picks a free host port, and tears everything down afterwards; it takes a few minutes because it restarts services mid-order.

```text
packages/contracts    messages, outcomes, and the outcome script shared by every service
packages/bus          the message-bus port, its in-memory and RabbitMQ adapters, the consume loop
packages/stage_kit    stage runtime: runner, simulator, handler contract, settings
services/orchestrator state machine, store port and adapters, API, dispatcher, timeline page
services/stage        the three stage handlers and the entrypoint
tests/contract        one suite per port, run against every adapter
tests/e2e             acceptance and restart tests against the Compose stack
```
