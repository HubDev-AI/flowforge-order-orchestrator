# Dramatiq experiment

This branch replaces the orchestrator's own execution layer with Dramatiq, the same experiment as the Celery branch (`experiment/celery-rewrite`, pull request #16) with a different library. It is for local testing and comparison, not for merge.

## What changed

| Part | `main` | This branch |
|---|---|---|
| Starting an order | The API stores the order; a dispatcher loop polls Postgres and publishes the first command | The API stores the order and enqueues a Dramatiq `pipeline` of one message per stage, each on the stage's queue `stage.<name>` |
| Running a stage | A stage service consumes its queue through the bus package and publishes a result | A Dramatiq worker per stage runs the `flowforge.stage` actor with the same simulator and handlers |
| Retries and backoff | The pure `transition` function sets a due time; the dispatcher re-publishes when it is due | The actor raises `dramatiq.Retry(delay=…)`; the message waits in Dramatiq's delay queue. The attempt number is the message's retry count |
| Deadlines | Set when the attempt is dispatched; queue time counts; a sweep times out silent attempts | Checked by the worker against the time the attempt started; a message time limit five seconds later is only a backstop |
| Compensation | The transition function moves the order to `compensating`; the dispatcher sends undo commands | The failing actor reports the failure and raises an exception listed in `throws`, which stops the pipeline without a retry; the recorder then enqueues a pipeline of undo messages in reverse, each carrying the output of the execute it reverses |
| History and state | The orchestrator writes every attempt and order change | Stage workers send an event when an attempt starts and ends to the `orchestrator.records` queue; a `recorder` service, run from the orchestrator image, is the only writer of order state and history. Stage workers have no database access |
| Retry policy | Read from the current config when an attempt fails | Copied into each message at submission (`max_retries`, deadline), so a config change affects only new orders |

Unchanged: the API and its responses, idempotency, the body and script limits, the timeline page, the outcome script, the simulator, the stage handlers, the schema, and the Compose commands. The trimmed store and API tests are the same as on the Celery branch. Unlike the Celery branch, where each stage worker writes to Postgres, this branch keeps a single writer.

## Size

| | `main` | Celery | Dramatiq |
|---|---:|---:|---:|
| Production Python lines | 1,915 | 1,249 | 1,349 |
| Library integration (worker, recorder, launcher, message contract) | n/a | 421 | 512 |
| Test lines | 3,714 | 1,895 | 1,966 |

The Dramatiq branch is larger than the Celery one because it keeps a single database writer: the recorder actor, its event handling, and the guards for late events.

## The original end-to-end suite against this branch

`tests/e2e/` is unchanged from `main`. 14 of 15 tests pass, the same 14 as on the Celery branch. The failure is the same: `test_only_the_live_attempt_runs_when_a_stage_comes_back_after_timeouts`. While a stage's worker is down its message waits in the queue, nothing records an attempt or a timeout, and the order stays `running` until the worker returns. The unit suite (99 tests) and the adapter contract suite (17 tests) pass.

## Dramatiq against Celery, as built

| | Celery branch | Dramatiq branch |
|---|---|---|
| RabbitMQ 4.3 | Worker coordination (remote control, mingle, gossip) had to be switched off, because its transient queues are rejected | Starts unchanged; every queue it declares is durable |
| Acknowledgement | Before the task by default; switched to after (`task_acks_late`) | After processing by default |
| Unexpected errors in a task | Fail at once; the order is left where it was | Retried by Dramatiq like any exception, within the stage's attempt cap |
| Retry delay | `self.retry(countdown=…)` | `raise Retry(delay=…)`; the default backoff (15 s minimum, with jitter) is bypassed by the explicit delay |
| Compensation trigger | The chain's `link_error` errback, run inside the failing worker | The actor enqueues the undo pipeline itself. Dramatiq's `on_failure` callback fires after every failed run, including runs that will be retried, so it cannot mean "gave up" |
| Deadline including queue time | Not available | `AgeLimit` exists but measures from the message's first enqueue and a retry keeps that timestamp, so a retry after a timed-out attempt would be dropped; it is not used |
| Publisher confirms | Off by default; turned on with `confirm_publish` | Off by default; turned on with `confirm_delivery=True` |
| Failed messages | Acknowledged and gone | Dead-lettered to `stage.<name>.XQ` and kept for seven days, so a stuck order's message can be inspected |
| Tooling | `celery inspect` and Flower, unavailable here | Prometheus middleware available; no remote inspection |

## What is lost against `main`

The same as on the Celery branch:

- **Deadlines do not include time in the queue,** so a stage that is down is invisible until it returns.
- **No recovery of lost work.** A crash between storing an order and enqueueing its pipeline leaves it `pending`. On `main` the sweep recovers it.
- **State updates are asynchronous.** The order's state lags each attempt by one hop through the records queue. One recorder consumes that queue in order, but a recorder retry after a database error delivers its event late; the writes are guarded so a late start cannot reopen a finished attempt or move the order back, and a finish that arrives first creates the attempt row.
- **Compensation depends on the recorder.** If the recorder records a permanent failure and crashes before enqueueing the undo pipeline, the order stays `compensating`.
- **Retry state lives in the broker,** as a message in the delay queue.

## Verdict

Dramatiq is the better of the two libraries for this system: it worked with RabbitMQ 4.3 without changes (publisher confirms aside, which both libraries leave off by default), acknowledges after processing, retries unexpected errors, and keeps failed messages in a dead-letter queue. It took slightly less code than Celery. It does not change the result that matters: both libraries pass the same 14 end-to-end tests and fail the same one, because neither notices work that is waiting or lost. That recovery is the part of `main` a task queue does not replace.
