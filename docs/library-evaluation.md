# Library evaluation: own dispatcher, Celery, Dramatiq

The first version of this system ran its stages with its own dispatcher, results consumer, and RabbitMQ adapter. Before settling the design, the architect built the same system on Celery and on Dramatiq and measured both against the same end-to-end suite. This file records that comparison and why the system now runs on Dramatiq. The current design is in [`design.md`](design.md).

The Celery prototype is on branch `experiment/celery-rewrite` (pull request #16) and was not merged.

## The three versions

| Part | Own dispatcher (first version) | Celery prototype | Dramatiq (current) |
|---|---|---|---|
| Starting an order | The API stores the order; a dispatcher loop polls Postgres and publishes the first command | The API stores the order and sends a Celery `chain` | The API stores the order and enqueues a Dramatiq `pipeline` of one message per stage, each on the stage's queue |
| Running a stage | A stage service consumes its queue through a bus package and publishes a result | A Celery worker per stage | A Dramatiq worker per stage runs the `flowforge.stage` actor |
| Retries and backoff | A pure transition function sets a due time; the dispatcher re-publishes when it is due | `self.retry(countdown=…)` | `raise Retry(delay=…)`; the attempt number is the message's retry count |
| Deadlines | Set at dispatch, so queue time counts; a sweep times out silent attempts | Celery's soft time limit, from the start of the task | Checked against the start of the attempt; a time limit five seconds later is a backstop |
| Compensation | The transition function moves the order to `compensating`; the dispatcher sends undo commands | The chain's `link_error` errback | The recorder enqueues the undo pipeline when a stage reports a permanent failure |
| History and state | The orchestrator writes every attempt and order change | Each stage worker writes to Postgres | Stage workers send events; one recorder service writes |
| A stage that is down | Attempts time out every deadline and spend the retry budget | The order waits with no attempt and nothing notices | A visible wait with a maximum, then the order is given up |

## Size

| | Own dispatcher | Celery | Dramatiq, current |
|---|---:|---:|---:|
| Production Python lines | 1,915 | 1,249 | 1,599 |
| Execution layer (dispatcher, results, bus, runner; or the library integration) | 695 | 421 | 698 |
| Test lines | 3,714 | 1,895 | 2,210 |

The Dramatiq version is larger than the Celery prototype because it keeps a single database writer (the recorder, its events, and its guards for late events) and implements the waiting policy. Without that policy it was 1,349 lines.

## End-to-end results

The end-to-end suite drives the stack only through the API. It ran unchanged against each library first:

| Version | Result |
|---|---|
| Own dispatcher | 15 of 15 |
| Celery prototype | 14 of 15 |
| Dramatiq, before the waiting policy | 14 of 15 |
| Dramatiq, current | 16 of 16, after one test was replaced |

Both libraries failed the same test, `test_only_the_live_attempt_runs_when_a_stage_comes_back_after_timeouts`. It encodes the first version's policy: while a stage is down, its attempts time out one after another. A task library has no clock running while no worker consumes, so the work simply waits in the queue. Reproducing that policy on a library means a record of every send, a sweep, and a stale-message check: the dispatcher again.

The current version changes the policy instead. A stage that is down is a visible wait with a maximum, described in [`design.md`](design.md) under "Recovery policy". The old test is replaced by two that encode it:

- `test_a_stage_that_is_down_holds_the_order_visibly_until_it_returns`: with fraud stopped, the order reports that it is waiting at fraud with no fraud attempt; when fraud returns, attempt 1 runs and the order completes.
- `test_a_stage_down_longer_than_its_max_wait_gives_the_order_up`: with fraud's maximum wait set to 8 seconds and fraud stopped, the order ends `failed` with the reason `fraud unavailable: no worker took it within 8 s`, inventory is undone, and when fraud returns it drops the stale work without running it.

All 16 pass locally and in CI, with the unit suite (108 tests) and the contract suite (23 tests, against real Postgres).

## Dramatiq against Celery, as built

| | Celery | Dramatiq |
|---|---|---|
| RabbitMQ 4.3 | Worker coordination (remote control, mingle, gossip) declares transient, non-exclusive queues that 4.3 rejects, so it had to be switched off | Starts unchanged; every queue it declares is durable |
| Acknowledgement | Before the task by default; switched to after (`task_acks_late`) | After processing by default |
| Unexpected errors in a task | Fail at once unless configured | Retried like any exception, within the stage's attempt cap |
| Retry delay | `self.retry(countdown=…)` | `raise Retry(delay=…)`; the default backoff (15 s minimum, with jitter) is bypassed by the explicit delay |
| Compensation trigger | The chain's `link_error` errback, run inside the failing worker | Started by the recorder. Dramatiq's `on_failure` callback fires after every failed run, including runs that will be retried, so it cannot mean "gave up" |
| Deadline including queue time | Not available | `AgeLimit` measures from a message's first enqueue and a retry keeps that timestamp, so it would drop retries; a handoff stamp replaces it |
| Publisher confirms | Off by default; turned on with `confirm_publish` | Off by default; turned on with `confirm_delivery=True` |
| Failed messages | Acknowledged and gone | Dead-lettered to `stage.<name>.XQ` and kept for seven days |
| Tooling | `celery inspect` and Flower, unavailable here | Prometheus middleware available; no remote inspection |

## What the move changes

- **An outage is a waiting state, not failed attempts.** Attempts mean executions only: a retry or a deadline counts against `max_attempts`, and an outage does not.
- **Retries, backoff, chaining, and redelivery are the library's.** The application keeps the policy: the backoff it asks for, the deadline, compensation, and the maximum wait.
- **State updates are asynchronous.** The order's state lags each attempt by one hop through the records queue, under guards against late events.
- **Retry state lives in the broker,** as a message in Dramatiq's delay queue rather than a due time on the order row.

## Verdict

Dramatiq is the better of the two libraries for this system, and with the waiting policy it passes every scenario with about 300 fewer production lines than the first version (1,599 against 1,915). The trade is a different recovery policy for a stage that is down: a bounded, visible wait instead of spent retries, which is also the better behaviour for an outage.
