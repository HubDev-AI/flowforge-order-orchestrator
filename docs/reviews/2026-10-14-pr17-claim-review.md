# PR #17: accuracy of the orchestration rationale

Reviewed 2026-10-14. PR #17 head: `c6307fa8503f501017c8060ca614310c7c53583b`. Celery experiment PR #16 head: `b1282ebd506b51dc1c29a3ccaab3a3da7ffefe38`.

Verdict: the central design rationale is defensible, but the current wording needs correction before being used as an interview explanation. It generalizes shortcomings of one prototype into claims about all task-queue implementations and overstates protection from stale execution.

Scope: read PR #17's full diff, its committed implementation where relevant, PR #16's experiment report, task configuration, execution and workflow code, and the failed E2E scenario's assertions. Checked official Celery, Dramatiq and Temporal documentation/source, including Dramatiq through Context7. No new test run. No remote comments or edits. The local workspace is undergoing a separate Dramatiq experiment; reviewed PR code through immutable GitHub refs rather than changing that checkout.

## Corrections

### P2 — “nothing notices work that is stuck or lost” is false as a blanket library claim

Locations: `README.md:213`; `docs/design.md:86`, `:95` and PR body.

Celery offers execution time limits, retries and worker-loss redelivery options. PR #16 itself sets `task_acks_late=True` and `task_reject_on_worker_lost=True`, and its signatures set soft/hard execution limits. Dramatiq also has retry, processing-time-limit and broker redelivery behavior. These do not automatically maintain this application's order-level progress, but they contradict the statement that nothing handles work lost during execution. [Celery task documentation](https://docs.celeryq.dev/en/stable/userguide/tasks.html).

Correction: “The prototype did not implement a persisted order deadline that includes queue waiting and advances the order while no stage consumer is available. Task retries and execution time limits alone do not provide that application policy.”

### P2 — “late or stale work is harmless” exceeds the implementation's guarantee

Location: `docs/design.md:101`.

The broker adapter checks a command's deadline before yielding it; the stage runner then awaits the handler without cancelling or fencing it at the deadline. A handler that began before expiry can continue after the orchestrator has timed out that attempt and started retry or compensation. A stale result cannot settle a closed attempt, but an external effect can still occur. This is especially material when replacing mock handlers with real reservation/payment/shipping operations.

Correction: “Expired queued commands are skipped and stale results cannot update workflow state. Already-running handlers can still produce effects after their deadline, so real handlers require idempotency and, where necessary, fencing or reconciliation.”

### P2 — The prototype does not prove a full custom dispatcher is necessary

Locations: `docs/design.md:103` and `:107`.

The experiment shows a useful size reduction and a specific difference in deadline behavior. It does not prove every Celery implementation would reproduce the entire current dispatcher. Lost-before-enqueue can be addressed with a transactional outbox or persisted launch intent; application deadline reconciliation can be narrower than this dispatcher. Those add complexity, but its size has not been established by the reported prototype.

The prototype's unexpected-error behavior is partly its own policy: `compensate` logs non-`PermanentFailure` exceptions and returns. Celery did not require that choice. Separately, the original assignment requires graceful asynchronous failure handling but does not prescribe this exact policy of burning attempts while a stage is unavailable. The failed E2E test requires two recorded timeouts before restarting the fraud service, then execution of attempt 3 and two stale-command log entries. Queueing safely until the service returns is a different policy, not inherently broken task-queue behavior.

Correction: “Matching our queue-inclusive deadline and history policy would require additional persisted recovery logic on top of the prototype. We retained the existing implementation because it already provides and tests that policy.”

### P3 — Attribute experimental results to their recorded evidence

Location: `docs/design.md:103` and `README.md:213`.

The numbers 14/15 E2E and 1,915→1,249 production lines match PR #16's committed `docs/celery-experiment.md`. They were not independently reproduced here. PR #16's returned checks show lint succeeded and the test matrix skipped; they do not supply CI confirmation of the reported E2E run. Use “the recorded local experiment reported…” and link the exact report/commit and, ideally, a retained test log. The documented result is plausible and matches inspected code, but should not be presented as independent comparative proof.

Experiment record: [PR #16 report at reviewed commit](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/b1282ebd506b51dc1c29a3ccaab3a3da7ffefe38/docs/celery-experiment.md).

## Parts that are correct, with qualifications

- A committed order row with due time allows the current dispatcher to discover work after an API crash before publication. There remains a later database/broker boundary, recovered using persisted attempts and deadlines.
- Current attempt deadlines include queue wait. Expiry applies the retry/compensation policy; it does not necessarily schedule another execute attempt when the budget is exhausted.
- Dramatiq's stock `AgeLimit` checks in `before_process_message`, marks the expired message failed and raises `SkipMessage`. It does not independently update orders while a consumer is absent. [AgeLimit source, v2.2.1](https://github.com/Bogdanp/dramatiq/blob/v2.2.1/dramatiq/middleware/age_limit.py).
- The stock `on_failure` callback is in `after_process_message`; the skipped-message path emits a separate `after_skip_message` hook. Thus the ordinary failure callback is not invoked by this skip, but custom skip handling is possible. “No built-in order update” is more precise than “stops silently”: the library logs the skip. [Worker source](https://github.com/Bogdanp/dramatiq/blob/v2.2.1/dramatiq/worker.py), [Callbacks source](https://github.com/Bogdanp/dramatiq/blob/v2.2.1/dramatiq/middleware/callbacks.py).
- Dramatiq declares durable ordinary, delay and dead-letter queues in its RabbitMQ adapter. That avoids the cited transient-queue issue, but is not a complete tested compatibility claim. Publisher confirms are disabled by default, so matching this app's publication behavior requires explicit configuration. [RabbitMQ adapter source](https://github.com/Bogdanp/dramatiq/blob/v2.2.1/dramatiq/brokers/rabbitmq.py).
- Temporal's schedule-to-close timeout includes queue wait and spans the entire activity execution across retries. It is not a direct one-to-one equivalent of each current attempt's deadline. Making the workflow authoritative avoids a separate order-write/start gap only under that architecture; an independent application database write followed by workflow start would still need coordination. [Temporal timeout documentation](https://docs.temporal.io/encyclopedia/detecting-activity-failures).
- “Dramatiq is the better fit” is a reasonable engineering preference to explain, not a fact established by the Celery experiment. Stock retries and compatibility conveniences alone do not constitute a behavior-parity evaluation.

## Suggested replacement explanation

Celery and Dramatiq provide task execution, retries, composition and worker-failure handling. Our additional policy is that each recorded attempt has a deadline covering queue wait, and the order advances through retry or compensation even while the stage service is unavailable. The recorded Celery prototype reduced code and passed most existing E2E scenarios, but did not preserve that deadline policy or close the database-to-enqueue gap. Additional durable handoff and deadline reconciliation could supply those behaviors; we retained the existing database-driven dispatcher because it already implements and tests them. Stale results cannot change order state, while safe external side effects still depend on handler design.
