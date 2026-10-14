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
| Production Python lines | 1,915 | 1,249 | 1,599 |
| Library integration (worker, recorder, launcher, message contract and middleware) | n/a | 421 | 698 |
| Test lines | 3,714 | 1,895 | 2,210 |

The Dramatiq branch is larger than the Celery one for two reasons: it keeps a single database writer (the recorder service, its events, and the guards for late events), and it implements the waiting-for-a-worker policy described below. Without that policy it was 1,349 lines.

## The waiting-for-a-worker policy

`main` treats a stage that is down as failed attempts: each attempt times out after its deadline and the retry budget is spent while nothing is running. This branch treats it as a visible waiting state instead, which a task library supports without rebuilding the dispatcher:

- **Every handoff is timestamped.** A Dramatiq middleware stamps stage work with the time it becomes available to a worker, on every enqueue, including a pipeline's next stage and a delayed retry. The recorder stores the same moment on the order as `waiting_since`.
- **The wait is visible.** Status reports `waiting_for_worker_since` once handed-off work has gone two seconds without a worker starting it, and the page shows "Waiting since … for a fraud worker to pick up the work". No attempts are recorded or spent while waiting.
- **The wait is bounded.** Each stage has `max_wait_seconds` (60 by default). A loop in the orchestrator reads the orders that are waiting and, five seconds past the stage's maximum, sends the recorder a give-up event. The recorder ends the wait: the stages that ran are undone in reverse, the waiting stage is undone only if it ever ran, and an undo that cannot be picked up ends the order in `compensation_failed`. The terminal reason says, for example, `fraud unavailable: no worker took it within 60 s`.
- **Stale work never runs.** A worker drops stage work that waited longer than its stage's maximum, so a stage that returns after the order was given up does not act on it. The five-second margin lets a worker that started just in time report its start before the order is given up.
- **Lost before handoff is covered by the same loop.** An order whose first message never reached the queue waits with no attempt and is given up after the maximum wait. It ends `failed` with a reason, instead of `pending` for ever.

Attempts now mean executions only: a retry or a deadline counts against `max_attempts`, and an outage does not.

## End-to-end results

`tests/e2e/` started unchanged from `main`, as the referee. Against the library alone, 14 of 15 passed; the failure was `test_only_the_live_attempt_runs_when_a_stage_comes_back_after_timeouts`, which encodes `main`'s policy of timing out attempts while a stage is down. With the waiting policy, that test is replaced on this branch by two tests that encode the new policy:

- `test_a_stage_that_is_down_holds_the_order_visibly_until_it_returns`: with fraud stopped, the order reports that it is waiting at fraud with no fraud attempt; when fraud returns, attempt 1 runs and the order completes.
- `test_a_stage_down_longer_than_its_max_wait_gives_the_order_up`: with fraud's maximum wait set to 8 seconds and fraud stopped, the order ends `failed` with the reason `fraud unavailable: no worker took it within 8 s`, inventory is undone, and when fraud returns it drops the stale work without running it.

All 16 end-to-end tests pass locally, with the unit suite (108 tests) and the adapter contract suite (23 tests, against real Postgres). Replacing a referee test is legitimate here only because the policy changed deliberately; the old test still describes `main`.

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

## What differs from `main`

- **An outage is a waiting state, not failed attempts.** This is a policy change, described above; `main` would need the same change to stay consistent with this branch.
- **State updates are asynchronous.** The order's state lags each attempt by one hop through the records queue. One recorder consumes that queue in order, but a recorder retry after a database error delivers its event late; the writes are guarded so a late start cannot reopen a finished attempt or move the order back, and a finish that arrives first creates the attempt row.
- **Compensation depends on the recorder.** If the recorder records a permanent failure and crashes before enqueueing the undo pipeline, the order stays `compensating` until its wait passes the maximum and the loop gives it up.
- **The maximum wait is read from the current config.** The worker reads it from the order's snapshot in the message; the orchestrator's loop reads the current config, so a config change mid-order can make the two disagree by the size of the change.
- **Clocks must agree.** The worker's drop and the loop's give-up compare timestamps from different containers, as `main`'s deadlines do; the five-second margin absorbs small drift.
- **Retry state lives in the broker,** as a message in the delay queue.

## Verdict

Dramatiq is the better of the two libraries for this system: it worked with RabbitMQ 4.3 without changes (publisher confirms aside, which both libraries leave off by default), acknowledges after processing, retries unexpected errors, and keeps failed messages in a dead-letter queue. On its own it passed the same 14 end-to-end tests as Celery and failed the same one, because a task library does not notice work that is waiting for a worker.

With the waiting-for-a-worker policy, the Dramatiq branch passes every end-to-end test, about 300 lines smaller than `main` (1,599 against 1,915). The price is a different recovery policy: an outage holds the order visibly and gives it up after a maximum wait, rather than spending its retries. That policy is arguably the better one for a stage that is down, and it is the change that would let `main` hand retries to a library as well.
