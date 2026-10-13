# flowforge-order-orchestrator

An order is submitted through a REST API and runs through a configurable sequence of slow, sometimes failing stages (inventory, fraud, shipping) until it lands in a terminal state. Anyone can ask where an order is and how it got there, in the API or on a small timeline page. The stages are simulated; the point is the orchestration, the state model, and the visibility of the workflow.

## Architecture

```mermaid
flowchart TB
  client[Browser timeline page or API client] -->|HTTP| api[Orchestrator API]
  api --> store[(Order store port)]
  disp[Dispatcher loop] --> store
  disp -->|command per attempt| bus[(Message bus port)]
  results[Results consumer] --> store
  bus -->|results| results
  bus -->|stage.inventory| s1[Stage: inventory]
  bus -->|stage.fraud| s2[Stage: fraud]
  bus -->|stage.shipping| s3[Stage: shipping]
  s1 & s2 & s3 -->|result| bus
  store -.->|adapter| pg[(Postgres)]
  bus -.->|adapter| mq[(RabbitMQ)]
```

- **Orchestrator** (FastAPI). Owns the state machine and is the only process that touches the database. It serves the API and the timeline page, and runs two background loops: a dispatcher that publishes one command per stage attempt, and a results consumer that applies each stage's answer.
- **Stage services.** One container per stage, all from one image; `STAGE_NAME` picks the handler. They hold no state: a command carries everything the stage needs, and a result carries everything it produced.
- **RabbitMQ** carries commands (a durable queue per stage) and results (one queue). **Postgres** holds each order and an append-only log of every attempt, which is the history the API and the page show.
- **Ports.** The bus and the store sit behind two small interfaces. Postgres and RabbitMQ are the shipped adapters; in-memory adapters are the second implementation of each, so the state machine and the application are tested with no infrastructure.

An order's life: submit (`pending`), the dispatcher claims it and publishes the first stage's command (`running`), each success advances it to the next stage, and the last success completes it. A transient failure or a missed deadline is retried with backoff up to a cap. A permanent failure runs the undo of every stage already dispatched, in reverse, and the order ends `failed` (every undo succeeded) or `compensation_failed` (an undo gave up).

## Build and run

You need Docker with Compose v2. Nothing else is required to run the system.

```sh
docker compose up -d --build --wait
```

This builds two images from one `Dockerfile` (the `orchestrator` and `stage` targets), starts Postgres, RabbitMQ, the orchestrator, and three stage services, applies the database migration, and returns once everything is healthy.

- Timeline page: <http://localhost:8000>. Submit an order; you are redirected to its own URL and the page polls until the order finishes.
- API: `http://localhost:8000/api/orders`.
- If port 8000 is taken, set `ORCHESTRATOR_PORT` first (`ORCHESTRATOR_PORT=18000 docker compose up -d --build --wait`).
- Stage logs show one line per processed command: `docker compose logs -f stage-fraud`.
- Stop with `docker compose down`. Data survives in named volumes; `docker compose down -v` wipes it.

### Try the API

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

The page has a field for the same script. Some useful ones: `{"fraud": {"execute": ["reject"]}}` shows compensation, `{"fraud": {"execute": ["timeout"]}}` shows a missed deadline and the retry that follows.

## Design decisions and trade-offs

The reasoning behind each choice, the alternatives that were rejected, and where the build refined the plan are in [`docs/design.md`](docs/design.md). The planning record, with risks and what was deliberately not built, is [`docs/plans/`](docs/plans/), and the research behind it is [`docs/research/`](docs/research/). In short:

| Decision | What it buys | What it costs |
|---|---|---|
| The orchestrator owns the state machine and commands each stage over a broker | One place holds the rules, so history and status come for free | A single coordinator: one instance only, and the dispatcher is not safe to replicate |
| A pure transition function; retries and timeouts only write rows | Every edge of the state diagram is a unit test, and a backoff survives a restart | Recovery is driven by polling, so it is only as fast as the sweep interval |
| One sweep recovers both lost dispatches and silent stages, using a deadline on every command | One mechanism instead of an outbox plus a watchdog | A dead stage costs one attempt and one deadline window (10s by default). A command the bus could not send (connection down) is released and does not count. A publish whose confirmation is lost may have reached the broker, so it keeps its attempt and its deadline recovers it |
| RabbitMQ with acknowledge-before-handler, and no broker retries or dead-letter queue | A single retry authority, the orchestrator, so attempts are counted once | A stage that crashes mid-handler loses that command until its deadline fires |
| An order row plus an append-only attempt log, in Postgres | The visible history is the log, and several containers can write safely | No event replay or time-travel; SQLite was rejected because it is not safe across processes |
| An atomic store port: claim, time out, and settle each take a pure callback | An attempt row and its order update land together or not at all, so a crash cannot strand an order | The port is less obvious than separate calls, and the Postgres adapter must keep it in one transaction |
| Undo hooks on every stage, defaulting to a no-op | The compensation path exists and is tested even though simulated stages have no side effects | Extra moving parts for work that is, today, simulated |
| At-least-once delivery, idempotent handlers, and a guarded close of each attempt | A duplicate or late result is harmless and an outcome is applied once | Stage handlers must be idempotent per order and stage |
| One stage image selected by `STAGE_NAME`; a stage list in TOML copied onto each order | Adding a stage is a handler, a Compose service, and a config line; in-flight orders are unaffected by config changes | An order already in flight keeps its old stage list until it finishes, so a removed stage's service must keep running until those orders are done; retry settings are read from the current config, not snapshotted |
| A static timeline page with no build step | Nothing to install or build; the page is one file | No component framework, and browser behaviour is checked by hand, not in CI |

### Consistency, concurrency, and load

| Property | Guarantee | Ceiling |
|---|---|---|
| Delivery | At-least-once for commands and results (persistent messages, durable queues, publisher confirms). Settlement is effectively once: a guarded close applies one outcome per attempt, so a duplicate or late result is dropped | A stage that crashes mid-handler loses that command until its deadline; a duplicate command runs the handler twice, which is why handlers are idempotent per order and stage |
| Durability | An attempt row and its order update commit in one transaction; a backoff is a due time on the row, so it survives a restart | Postgres is one node with a named volume; no replication |
| Submission | Linearizable per idempotency key through a unique constraint; the losing concurrent insert re-reads and replays; SHA-256 of canonical JSON decides replay versus conflict | Replay is checked against the stored order before today's config, so tightening the pipeline never rejects a replay; it returns the current state, not the original response; keys never expire; the timeline page reuses one key while the user retries the same body; a body is at most 64 KiB, counted as it arrives and a script lists at most `max_attempts` tokens per stage and kind |
| Status | One snapshot: the order and its attempts are read in one `REPEATABLE READ` transaction, so state and history never disagree and the payload is read once | Read from the primary, polled once a second per open page |
| Concurrency | One orchestrator process with two loops, serialized per order by row locks: claims use `FOR UPDATE SKIP LOCKED`, settlement a blocking `FOR UPDATE`, so a result waits for a concurrent timeout of its order and is then dropped as stale. Stages run concurrently across orders, strictly in sequence within one order | The results consumer handles one result at a time (prefetch 1), about 1 / settle latency per second. The dispatcher claims batches of 50 and ticks again at once while a batch is full. Stage services scale by adding replicas (competing consumers, no shared state); the orchestrator does not |
| Load | The payload travels in every command, including undo commands | Bytes on the bus per order are payload x stages x attempts x 2 at worst, bounded by the body cap. History is append-only with no retention; per-order rows are bounded at stages x `max_attempts` x 2 |
| Clocks | Deadlines are absolute times set by the orchestrator and compared on the stage's clock | Containers must share a clock; a skew larger than the 10 ms margin lets a scripted timeout succeed early |
| Availability | Submission needs the database only; progress needs the broker too | Orders accepted during a broker outage wait; nothing counts against them while the bus reports itself down; only a command that may have reached the broker is counted, as it may also run. |

What is deliberately out of this version: more than one orchestrator instance, authentication, structured logs and metrics, a manual retry for terminal orders, and Redis and SQLite adapters. The full list is under "Limits" in the design record.

## Change the pipeline

The stage order, retry settings, sweep interval, and adapters are in [`config/pipeline.toml`](config/pipeline.toml). Per-stage overrides go under `[overrides.<stage>]`. An order keeps the stage list it was submitted with, so a config change affects only later orders.

To add a stage:

1. Add `services/stage/handlers/<name>.py` with a `Handler` class that implements `execute` (and `undo` if the stage has side effects to reverse).
2. Add a Compose service that runs the stage image with `STAGE_NAME: <name>` (copy `stage-fraud`).
3. Add `<name>` to `stages` in `config/pipeline.toml`.

The orchestrator and the stage kit need no change.

## Develop and test

Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not contract and not e2e"   # unit tests, no Docker
uv run pytest -m contract                     # adapters against real Postgres and RabbitMQ
uv run pytest -m e2e                          # builds and drives the whole Compose stack
```

CI (`.github/workflows/ci.yml`) runs lint and format checks on every pull request and the three test suites once a pull request is out of draft, so marking a draft ready for review starts them. The contract and end-to-end suites need Docker. The end-to-end suite runs its own Compose project, picks a free host port, and tears everything down afterwards; it takes a few minutes because it restarts services mid-order.

```text
packages/contracts    messages, outcomes, and the outcome script shared by every service
packages/bus          the message-bus port, its in-memory and RabbitMQ adapters, the consume loop
packages/stage_kit    stage runtime: runner, simulator, handler contract, settings
services/orchestrator state machine, store port and adapters, API, dispatcher, timeline page
services/stage        the three stage handlers and the entrypoint
tests/contract        one suite per port, run against every adapter
tests/e2e             acceptance and restart tests against the Compose stack
docs/                 brief, plan, research, design record, and the AI conversation logs
```

## AI assistants

Three tools were used across five jobs. The conversation logs are in [`docs/conversations/`](docs/conversations/) and the review reports in [`docs/reviews/`](docs/reviews/).

| Job | Tool and model | What it did | Log |
|---|---|---|---|
| Brainstorming, architecture, plan | Claude Fable 5.1, high effort, in Claude Code | Turned the brief into requirements, ran research on the broker client, retry and sweep patterns, and flow gaps (notes in `docs/research/`), proposed options with trade-offs, and wrote the plan | [`session-d7e11c2d-…`](docs/conversations/session-d7e11c2d-84dc-4bb3-ad51-a24eb20940f8.md) |
| Coding | Claude Sonnet 5.5, xhigh effort, in Claude Code | Implemented the plan one unit at a time, each in its own pull request, tests first where behaviour changed, checking that a test fails when the code it guards is broken. Looked up library docs (aio-pika, SQLAlchemy, Alembic, testcontainers) through Context7 rather than from memory, and drove Chrome to check the timeline page in light and dark mode and at phone width | [`session-69752424-…`](docs/conversations/session-69752424-d5e4-43b4-a438-da6a7eed3c45.md) |
| Code review | Claude Fable 5.1 in Claude Code (the `ce-code-review` skill, run inline) | Reviewed the twelve pull requests as one stack after the build: correctness, reliability, security, tests, and the consistency, concurrency, and load trade-offs; confirmed findings with probes and mutation tests. Twenty findings, each fixed in the review-fixes pull request or recorded as an accepted limit; the outcome of each is at the top of the report | [`2026-10-13-stack-review.md`](docs/reviews/2026-10-13-stack-review.md) |
| Simplification review | ChatGPT (model as the author ran it; the report does not record it) | An independent read of the same stack by a different vendor, after the build: twelve behaviour-preserving cuts, each checked against library documentation through Context7, applied in the review-fixes pull request | [`2026-10-13-ponytail-stack-review.md`](docs/reviews/2026-10-13-ponytail-stack-review.md), [library evidence](docs/reviews/2026-10-13-ponytail-library-evidence.md) |
| Second code review | Codex with the `ce:review` skill (model as the author ran it; the report does not record it) | Re-reviewed the stack after the first round of fixes: confirmed the twelve simplification cuts were applied and found seven new issues in the fixes (a body cap that trusted the header, a lost publish confirmation that reused an attempt, a release that rescheduled settled orders, replays judged against today's config, the status query cost, a page retry that minted a new key, and untested enum checks), all fixed in the same pull request | [`2026-10-13-ce-review-round-two.md`](docs/reviews/2026-10-13-ce-review-round-two.md) |

The coding assistant also reviewed each unit before its pull request went up. That caught several real problems (an unbounded header, a consumer that could die silently, a result racing its own deadline), but it was the same model that wrote the code, which is why the two reviews above are separate steps by a different model and a different vendor.

**What I did.** I set the direction and the constraints and made the main architecture choices, which are marked as such in the plan: a config-ordered linear pipeline, compensation built now, an own orchestrator over a broker, RabbitMQ, one container per stage, and history through the status API and the page. I read both review reports, chose which findings to fix, and review and merge every pull request myself.
