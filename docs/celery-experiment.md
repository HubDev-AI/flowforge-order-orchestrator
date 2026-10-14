# Celery experiment

This branch replaces the orchestrator's own execution layer with Celery, to see what a library gives and what it takes away. It is an experiment for local testing, not for merge.

## What changed

| Part | `main` | This branch |
|---|---|---|
| Starting an order | The API stores the order; a dispatcher loop polls Postgres and publishes the first command | The API stores the order and sends a Celery `chain` of one task per stage |
| Running a stage | A stage service consumes its RabbitMQ queue through the bus package and publishes a result | A Celery worker per stage consumes `stage.<name>` and runs the same simulator and handlers |
| Retries and backoff | The pure `transition` function sets a due time; the dispatcher re-publishes when it is due | The task calls `self.retry(countdown=…)`; the retry waits in RabbitMQ and the worker |
| Deadlines | Set when the attempt is dispatched; queue time counts; a sweep times out silent attempts | Celery's `soft_time_limit` per task, counted from when the task starts running |
| Compensation | The transition function moves the order to `compensating`; the dispatcher sends undo commands | The chain's `link_error` errback runs in the failing worker and sends a chain of undo tasks in reverse |
| History and state | The orchestrator writes every attempt and order change in one transaction per event | Each stage worker writes its attempt and the order's state to Postgres as the attempt starts and ends |
| Retry policy | Read from the current config when an attempt fails | Copied onto the order's chain at submission, so a config change affects only new orders |

Unchanged: the API and its responses, idempotency, the body and script limits, the timeline page, the outcome script, the simulator, the stage handlers, the schema, and the Compose commands.

Size of the change:

| | `main` | This branch |
|---|---:|---:|
| Production Python lines | 1,915 | 1,249 |
| The execution layer (dispatcher, results consumer, bus, runner, transitions) and its Celery replacement | 695 | 421 |
| Test lines | 3,714 | 1,895 |

## How to run it

Exactly as in the README: `docker compose up -d --build --wait`, then the page and the API on the same port. Stage logs are now Celery worker logs: `docker compose logs -f stage-fraud`.

## The original end-to-end suite against this branch

The suite in `tests/e2e/` is unchanged from `main` and drives the stack only through the API, so it is the referee.

| Result | Tests |
|---|---|
| Pass | idempotent resubmit and conflict; a clean run; scripted retries; exhaustion with undo; rejection with reverse undo; an undo that keeps failing; a scripted timeout; the not-found page; orchestrator restarts during an execute, during an undo, and during a backoff; a config change mid-order; a broker restart mid-run; adding a stage |
| Fail | `test_only_the_live_attempt_runs_when_a_stage_comes_back_after_timeouts` |

14 of 15 pass. The failure is by design of the library: while a stage's worker is down its task waits in the queue, no attempt is recorded and no deadline fires, so the order stays `running` until the worker returns, and then attempt 1 runs. On `main` the sweep times out each attempt and the returning stage drops the stale commands.

The unit suite (98 tests) and the adapter contract suite (16 tests, against real Postgres) pass. The contract suite now tests the worker's SQL recorder; the RabbitMQ adapter tests are gone with the adapter.

## What is lost

- **Deadlines no longer include time in the queue.** A stage that is down, or a backlog, is invisible to the order: it shows `running` with no attempt until a worker picks the task up.
- **No recovery of lost work.** If the API crashes between storing an order and sending its chain, the order stays `pending` for ever. If a task fails for an unexpected reason (a bug, or Postgres refusing the attempt record), the order stays in its last state. This happened during the build: a missing import path left orders in `running`. On `main` the sweep recovers both cases. A Celery beat job that finds stuck orders would be the fix; it is not built.
- **Stage services are no longer stateless.** Every stage worker connects to Postgres and writes order state, and the recorder's SQL duplicates knowledge of the schema the orchestrator owns.
- **Retry state lives in the broker.** A retry waiting out its backoff is a message held by a worker, not a due time on the order row.
- **Celery's worker coordination is switched off.** Celery 5.6 declares transient, non-exclusive queues for remote control, mingle, and gossip; RabbitMQ 4.3 rejects them because the feature is deprecated, so the workers run without them, and `celery inspect` and `celery control` are unavailable.

## What is gained

- **Less code to own.** Retries, backoff, time limits, chaining, and error callbacks are library features, and the dispatcher and results consumer are gone.
- **No polling between stages.** The next stage starts as soon as the previous one ends, without waiting for a sweep.
- **The orchestrator is out of the execution path.** Restarting it never affects an order in flight.
- **The retry policy is snapshotted per order**, alongside the stage list.

## Verdict

Celery covers the brief's happy path, retries, compensation, and restarts with about a third less production code. What it does not provide is the deadline-based recovery `main` is built around: noticing that work is stuck or lost and acting on it. Adding that back (a beat job, queue-time deadlines, a record of what was sent) would rebuild part of the dispatcher on top of Celery.
