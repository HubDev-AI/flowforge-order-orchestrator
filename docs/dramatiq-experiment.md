# Dramatiq experiment

This branch replaces the orchestrator's own execution layer with Dramatiq, the same experiment as the Celery branch (`experiment/celery-rewrite`, pull request #16) with a different library. It is for local testing and comparison, not for merge.

## What changed

| Part | `main` | This branch |
|---|---|---|
| Starting an order | The API stores the order; a dispatcher loop polls Postgres and publishes the first command | The API stores the order and enqueues a Dramatiq `pipeline` of one message per stage, each on the stage's queue `stage.<name>` |
| Running a stage | A stage service consumes its queue through the bus package and publishes a result | A Dramatiq worker per stage runs the `flowforge.stage` actor with the same simulator and handlers |
| Retries and backoff | The pure `transition` function sets a due time; the dispatcher re-publishes when it is due | The actor raises `dramatiq.Retry(delay=…)`; the message waits in Dramatiq's delay queue. The attempt number is the message's retry count |
| Deadlines | Set when the attempt is dispatched; queue time counts; a sweep times out silent attempts | Checked by the worker against the time the attempt started; a message time limit five seconds later is only a backstop |
| Compensation | The transition function moves the order to `compensating`; the dispatcher sends undo commands | The failing actor enqueues a pipeline of undo messages in reverse, then raises an exception listed in `throws`, which stops the pipeline without a retry |
| History and state | The orchestrator writes every attempt and order change | Each stage worker writes its attempt and the order's state to Postgres, as on the Celery branch |
| Retry policy | Read from the current config when an attempt fails | Copied into each message at submission (`max_retries`, deadline), so a config change affects only new orders |

Unchanged: the API and its responses, idempotency, the body and script limits, the timeline page, the outcome script, the simulator, the stage handlers, the schema, and the Compose commands. The worker's SQL recorder and the trimmed store and tests are the same as on the Celery branch, so the two branches differ only in the library integration.

## Size

| | `main` | Celery | Dramatiq |
|---|---:|---:|---:|
| Production Python lines | 1,915 | 1,249 | 1,230 |
| Library integration (worker, recorder, launcher, message contract) | n/a | 421 | 403 |
| Test lines | 3,714 | 1,895 | 1,867 |

## The original end-to-end suite against this branch

`tests/e2e/` is unchanged from `main`. 14 of 15 tests pass, the same 14 as on the Celery branch. The failure is the same: `test_only_the_live_attempt_runs_when_a_stage_comes_back_after_timeouts`. While a stage's worker is down its message waits in the queue, nothing records an attempt or a timeout, and the order stays `running` until the worker returns. The unit suite (95 tests) and the adapter contract suite (16 tests) pass.

## Dramatiq against Celery, as built

| | Celery branch | Dramatiq branch |
|---|---|---|
| RabbitMQ 4.3 | Worker coordination (remote control, mingle, gossip) had to be switched off, because its transient queues are rejected | Starts unchanged; every queue it declares is durable |
| Acknowledgement | Before the task by default; switched to after (`task_acks_late`) | After processing by default |
| Unexpected errors in a task | Fail at once; the order is left where it was | Retried by Dramatiq like any exception, within the stage's attempt cap |
| Retry delay | `self.retry(countdown=…)` | `raise Retry(delay=…)`; the default backoff (15 s minimum, with jitter) is bypassed by the explicit delay |
| Compensation trigger | The chain's `link_error` errback, run inside the failing worker | The actor enqueues the undo pipeline itself. Dramatiq's `on_failure` callback fires after every failed run, including runs that will be retried, so it cannot mean "gave up" |
| Deadline including queue time | Not available | `AgeLimit` exists but measures from the message's first enqueue and a retry keeps that timestamp, so a retry after a timed-out attempt would be dropped; it is not used |
| Failed messages | Acknowledged and gone | Dead-lettered to `stage.<name>.XQ` and kept for seven days, so a stuck order's message can be inspected |
| Tooling | `celery inspect` and Flower, unavailable here | Prometheus middleware available; no remote inspection |

## What is lost against `main`

The same as on the Celery branch:

- **Deadlines do not include time in the queue,** so a stage that is down is invisible until it returns.
- **No recovery of lost work.** A crash between storing an order and enqueueing its pipeline leaves it `pending`; a crash between recording a permanent failure and enqueueing the undo pipeline leaves it `compensating`. On `main` the sweep recovers both.
- **Stage workers write order state** to Postgres, and the recorder's SQL duplicates the schema.
- **Retry state lives in the broker,** as a message in the delay queue.

## Verdict

Dramatiq is the better of the two libraries for this system: it worked with RabbitMQ 4.3 without changes, acknowledges after processing, retries unexpected errors, and keeps failed messages in a dead-letter queue. It took slightly less code than Celery. It does not change the result that matters: both libraries pass the same 14 end-to-end tests and fail the same one, because neither notices work that is waiting or lost. That recovery is the part of `main` a task queue does not replace.
