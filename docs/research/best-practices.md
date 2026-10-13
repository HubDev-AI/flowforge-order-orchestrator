# Best practices: orchestration saga over RabbitMQ with Postgres state

Scope: implementation guidance for the settled design (orchestrator owns state and retries in Postgres, one command queue per stage, results queue back, deadline sweep, stage services stateless). Each item is tagged **Official**, **Community**, or **Inference** (reasoning from the settled constraints layered on official facts). No settled decision was found to be unworkable.

Skills checked: `~/.claude/skills/*` has no RabbitMQ/saga/outbox knowledge. `design-survey-house-pattern-first` applies in spirit (one retry mechanism, count every new clock/state), nothing more. No repo code to survey.

Deprecation check: aio-pika, FastStream, testcontainers-python, asyncpg, FastAPI all released within the last four months on PyPI; none deprecated. See section 9 for a pin caveat on aio-pika 10.x.

---

## 1. Where the stage worker acks: before the handler (deliberate deviation)

**Official** (RabbitMQ reliability guide): "A consuming application should not acknowledge messages until it has done whatever it needs to do with them." Unacked deliveries are requeued when the channel or connection closes; the `redelivered` flag is only a hint that the consumer *may* have seen it.

**Inference**: in this design the orchestrator is the sole owner of retries (attempt numbering, backoff, cap, deadline sweep). Ack-after-handler reintroduces a second, uncounted retry path:

- Stage crashes mid-handler -> broker requeues -> another stage replica (or the same one after restart) runs the *same attempt number* again, possibly after the orchestrator's sweep has already recorded `timed_out` and dispatched attempt N+1. Two handlers for one order run concurrently; two results arrive for attempt N.
- The orchestrator's attempt cap no longer bounds handler executions; broker redeliveries do not increment the orchestrator's attempt counter.
- On quorum queues the broker adds its own limits (`delivery-limit` default 20 in 4.x; `consumer_timeout` 30 min default, channel closed with `PRECONDITION_FAILED` on breach). Two retry authorities is exactly what the plan's "one mechanism" decision rejects.

**Recommendation**: in the stage template, ack immediately on delivery, *then* check the deadline, *then* run the handler, *then* publish the result. A stage crash loses the in-flight attempt; the sweep recovers it as `timed_out` and dispatches attempt N+1. This is at-most-once at the broker layer with at-least-once restored by the orchestrator, which is what the settled design already pays for.

The aio-pika idiom matters because two tempting spellings are wrong:

- Not `queue.consume(..., no_ack=True)` (automatic ack mode). **Official** (confirms guide): prefetch applies only in manual ack mode. In auto-ack the broker pushes the whole backlog into the client buffer, so a stage crash loses every buffered command, not one.
- Not `async with message.process():` in the stage consumer. It acks on context exit, which is ack-after.
- Use manual ack mode, `prefetch_count=1`, and `await message.ack()` as the first statement of the loop body. That combination gives one-at-a-time delivery with a one-message buffer and a bounded loss of exactly one command on crash.
- Orchestrator and stage must declare the command queue with identical arguments (durable classic, no extras), or the stage declares `passive=True`. Mismatched arguments raise `PRECONDITION_FAILED` and close the channel.

Two consequences the plan must spell out:

1. **Prefetch stops bounding concurrency once you ack early.** With ack-after, `prefetch_count` is the handler concurrency limit. With ack-before, the message is "done" from the broker's view the moment it's acked, so prefetch only bounds the client-side buffer. Bound handler concurrency explicitly: process sequentially (`async for message in queue.iterator()` and `await handler(...)` inline, with `prefetch_count=1`) or wrap the handler in an `asyncio.Semaphore`. Sequential with `prefetch_count=1` is enough for simulated stages and gives the simplest story: one in-flight command per stage container.
2. **Check the deadline when the handler would start, not at delivery.** A message can sit in the client-side prefetch buffer; evaluating the deadline on arrival lets an already-expired command run. With `prefetch_count=1` and sequential processing the gap is tiny, but the check belongs immediately before the handler call regardless.

Orchestrator side (results consumer) is the opposite: **ack after the DB commit** (Official guidance applies cleanly here because the handler is one idempotent DB write). See section 3 for the idempotency key.

If the plan prefers ack-after anyway: use classic queues (no delivery-limit; from RabbitMQ 4.3 `consumer_timeout` applies only to quorum queues, while on an image older than 4.3 classic queues still enforce the 30-minute timeout, which is irrelevant for ack-before), and make the orchestrator discard results whose attempt number is not the current one (section 3) so a broker redelivery can never advance the order twice.

**Prefetch for slow handlers (Official)**: confirms guide suggests 100-300 for most workloads and calls `prefetch_count=1` the most conservative; for handlers that take seconds and a single-consumer-per-container design, 1 is correct. Quorum queues cap prefetch at 2000 (irrelevant here).

## 2. Queue declaration, queue type, publisher confirms

- **Official**: `x-queue-type` defaults to `classic`; quorum must be requested explicitly. Quorum adds poison-message handling and `consumer_timeout`, both of which are broker-side retry authorities you do not want. With ack-before, **durable classic queues** are the correct, simpler choice for a single-node Compose broker. **Inference**.
- **Inference**: the orchestrator must declare every stage command queue (and the results queue) at its own startup, not rely on stage containers declaring them. Otherwise a publish to a stage whose container is down is unroutable. Pair with `mandatory=True` on publish: **Official** (reliability guide) says the broker then returns unroutable messages via `basic.return`; without it an unroutable publish is *confirmed* (confirms guide: "unroutable messages are confirmed once the exchange determines no queue matches") and vanishes silently. In aio-pika a returned mandatory message raises on `await publish(...)` only when the channel/connection is opened with `on_return_raises=True` (**Official** aio-pika publisher-confirms tutorial; the default is not to raise, the return is delivered to a `return_callbacks` handler). Set `on_return_raises=True` on the orchestrator's publish channel, catch `DeliveryError`/`TimeoutError`, log, and leave the row for the sweep. Either way the sweep covers it; the flag only makes the failure visible immediately.
- **Publisher confirms**: aio-pika channels default to `publisher_confirms=True` and `await exchange.publish(...)` resolves on the confirm (**Official** aio-pika docs). Nothing to add; just don't turn it off. Confirms are what make "commit, then publish, then trust the publish" sound for persistent messages to durable queues (**Official** confirms guide: ack arrives after the message is written to disk).
- Publish with `delivery_mode=PERSISTENT` on durable queues so a broker restart does not drop in-flight commands (otherwise the sweep handles it, but at deadline latency).

## 3. Idempotency and stale-result rejection

- **Official** (reliability guide): design consumers idempotent rather than relying on dedup tables; the `redelivered` flag is a hint, not a guarantee.
- **Inference / standard practice**: the attempts table gets a unique constraint on `(order_id, stage, attempt)`. The result consumer runs one transaction: `UPDATE attempts SET outcome=..., finished_at=now() WHERE order_id=? AND stage=? AND attempt=? AND outcome IS NULL` and `UPDATE orders SET state=... WHERE id=? AND current_stage=? AND current_attempt=? AND state='dispatched'`. Zero rows updated means stale or duplicate; ack and drop, log at debug. No separate dedup table; the state machine's conditional update *is* the dedup.
- Carry `(order_id, stage, attempt, deadline)` in the message body or headers and echo them back in the result so the orchestrator never has to infer which attempt a result belongs to. Set `message_id` to `f"{order_id}:{stage}:{attempt}"` as well; it costs nothing and makes management-UI debugging easier.
- Stage handlers are idempotent per `(order_id, stage)` (settled); the attempt number is for the orchestrator's bookkeeping, not for the handler.

**Broker TTL vs consumer deadline check (Official, TTL guide)**: per-message `expiration` only drops a message when it reaches the *head* of the queue; an expired message behind a live one stays queued and still counts in stats, and there is a documented race where a message expires after being written to the socket. So broker TTL cannot replace the consumer-side deadline check (already settled). Setting `expiration` equal to the deadline is a cheap optional extra that sheds backlog when a stage was down for a long time; it is not load-bearing. Note an expired message with a DLX configured gets dead-lettered, which is another reason to skip the DLX (next section).

## 4. Dead-letter exchange: skip it

**Official** triggers for dead-lettering: (1) `reject`/`nack` with `requeue=false`, (2) per-message TTL expiry, (3) queue length limit, (4) quorum-queue delivery-limit exceeded.

**Inference**: none fires in this design. Transient and permanent failures are *results* published to the orchestrator, not rejects. Expired commands are acked and discarded by the template, not rejected. No length limit. No quorum queues. The "exhausted retries" case lives in Postgres (`failed` state with full attempt history), which is strictly more useful than a DLQ because it is queryable and visible in the status endpoint. A DLX here either collects nothing or collects noise. YAGNI; it comes back only if the plan switches to ack-after on quorum queues (then route delivery-limit drops to a DLQ so nothing is lost silently).

Note the earlier session rationale for RabbitMQ ("native acknowledgement, redelivery, and dead-lettering mean less retry plumbing") is partially superseded by the later decision that the orchestrator owns retries. The broker's retry features are not used; the broker is a durable transport with per-stage queues. That is still a fine reason to pick it, but the plan should not claim DLX/redelivery as benefits it does not use.

## 5. Dual write: commit then publish; the sweep is the outbox relay

**Community consensus** (outbox literature): the dual-write hazard is a crash between the DB commit and the publish (lost command) or a publish before a commit that rolls back (ghost command). The outbox fix writes an outbox row in the same transaction and has a relay publish from it; relays are at-least-once, so consumers dedupe by id.

**Inference**: this design already has the outbox. The order row with `state='dispatched'`, `current_attempt`, and `deadline` is the outbox row; the sweep is the relay; re-dispatch increments the attempt. The only difference from a classic outbox is recovery latency: a lost publish is noticed after the deadline window instead of after the relay's polling interval. For an interview-scale system with deadlines of seconds, that is acceptable and removes a table, a relay loop, and a second set of tests. State this trade in the plan.

Rules that keep it correct:

- Order of operations for a dispatch: open transaction, record attempt row and set order to `dispatched` with deadline, **commit**, then publish. Never publish inside the transaction (holds the row lock across broker I/O; if the publish hangs on a disconnected channel, the sweep cannot claim the row either).
- Crash after commit, before publish: row is `dispatched` with a deadline; sweep re-dispatches as attempt N+1. Correct, and it costs one attempt from the cap. Acceptable; document it.
- Publish fails synchronously (`DeliveryError` from mandatory, or channel closed): don't roll back the attempt row, let the sweep handle it. One recovery path.
- A transactional outbox returns when: a second orchestrator instance exists, deadlines must be long (minutes) so sweep-latency recovery is too slow, or a lost dispatch must not consume an attempt. None apply now.

## 6. Sweep in a single asyncio FastAPI process

- **Official** (FastAPI lifespan docs): start the task before `yield` with `asyncio.create_task`, after `yield` call `task.cancel()` then `await task` under `contextlib.suppress(asyncio.CancelledError)`. Don't mix with `@app.on_event` (deprecated, and ignored when `lifespan=` is set). `TestClient`/`httpx.ASGITransport` only run lifespan when used as a context manager (`with TestClient(app) as c:` / `async with LifespanManager`), so tests that need the sweep must do that.
- **Inference**: wrap each tick in `try/except Exception: log.exception(...)`; an unhandled exception ends the `create_task` loop silently and the sweep stops forever with no symptom except orders never retrying. Use `asyncio.sleep(interval)` between ticks; no APScheduler, no fancy scheduler.
- **`FOR UPDATE SKIP LOCKED`**: with exactly one orchestrator the inter-worker benefit is moot, but it still earns its place as serialization between the sweep and the results consumer, which update the same `orders` row from different pool connections in the same process. Pattern per tick, one transaction: `SELECT id FROM orders WHERE (state='dispatched' AND deadline < now()) OR (state='waiting_retry' AND next_attempt_at <= now()) OR (state='pending' AND created_at < now() - interval ...) ORDER BY ... LIMIT n FOR UPDATE SKIP LOCKED`, then apply the state transition (bump attempt, set deadline/state), commit, then publish each command outside the transaction. Rows locked by an in-flight result commit are skipped and picked up next tick. Select ids in a subquery before the `LIMIT` if the row set grows (Community: Postgres may lock candidate rows before applying `LIMIT`). Index `(state, deadline)` and `(state, next_attempt_at)`.
- **Double dispatch on restart**: no special startup path. After a crash, every `dispatched` row still has its deadline; the sweep treats it like any other overdue row. If a stage was in fact still working on attempt N and reports after the sweep dispatched N+1, section 3's conditional update discards the stale result. The one window where both N and N+1 handlers run concurrently is inherent to deadline-based recovery and is why handlers are idempotent per `(order_id, stage)`. Document this; don't try to engineer it away.
- Advisory locks are the alternative and are the wrong tool here (Community): they need explicit release and lose the crash-rollback property of row locks.

## 7. Testing

- **State machine**: a pure function `transition(order_state, event) -> (new_state, effects)` where effects are data (`Dispatch(stage, attempt, deadline)`, `ScheduleRetry(at)`, `StartCompensation(...)`, `Finish(...)`). Tests assert the returned effects list; no DB, no broker, no clock (pass `now` in). Also test attempt-cap exhaustion -> compensation sequence in reverse, and stale-event rejection (event for attempt N when current is N+1 yields no change). This matches the settled "unit-testable with no broker or DB" requirement and makes the orchestrator's I/O layer a thin interpreter of effects.
- **Integration**: testcontainers-python 4.15 has a `DockerCompose` module (`from testcontainers.compose import DockerCompose`) that brings up the real compose file; use it with an override env file setting millisecond latencies, 1-2 s deadlines, and short backoff so the suite runs in seconds. This is preferable to `RabbitMqContainer` + `PostgresContainer` because the settled requirement is "drives the Compose stack through the API only". The `testcontainers[rabbitmq]` extra pulls `pika`; not needed with Compose. Wait on the orchestrator's health endpoint, not on container start.
- **Deterministic failure injection**: the per-order outcome script (settled) should be a list indexed by `(stage, attempt)` carried in the order payload, e.g. `{"fraud": ["transient", "transient", "ok"], "shipping": ["reject"]}`; the template looks up its stage and the attempt number from the command, falling back to the probabilistic profile when absent. Tests then assert the attempt history in `GET /orders/{id}` exactly. Add one test where the script says `"hang"` (handler never replies) to exercise the deadline sweep, and one where it says `"expired"` to confirm the template discards without running. Env-level profile knobs stay for the demo, the script is for tests.
- Keep the e2e suite small: happy path; transient-then-success; permanent rejection with compensation in reverse order; deadline timeout then success; idempotency key replay (same payload 200/201, different payload 409); orchestrator restart mid-flight (stop/start the container via compose, assert the order still completes).

## 8. aio-pika / FastAPI anti-patterns

- **One `connect_robust` per process**, opened in lifespan, closed after `yield`. Never a connection per request (handshake cost, file descriptors, and reconnect state is per connection). **Official** aio-pika docs: use `Pool` only if a single connection becomes a bottleneck.
- **Separate channels for consuming and publishing.** A channel-level error (unroutable mandatory publish, passive declare of a missing queue, ack with a bad tag, `PRECONDITION_FAILED`) closes the whole channel and every consumer on it. `RobustChannel` reopens and restores consumers, but in-flight deliveries on the closed channel are requeued (or, with ack-before, simply gone). Keep the results consumer on its own channel in the orchestrator; in the stage template, one channel for the command consumer and one for publishing results. **Official** (aio-pika error-handling docs + RabbitMQ confirms guide "acks must go back on the same channel that received the delivery").
- **Never block the event loop in a handler.** Simulated latency must be `await asyncio.sleep(...)`, never `time.sleep`. Heartbeats are sent at timeout/2 (default 60 s timeout, so ~30 s); a blocked loop misses two and the broker drops the connection (**Official** heartbeats guide; Community reports for pika). With ack-after that means redelivery storms; with ack-before it means lost in-flight work until the sweep. CPU-bound work goes to `asyncio.to_thread`. With asyncio handlers this is an easy rule, but the failure simulator is exactly where someone reaches for `random`+`time.sleep`.
- **aio-pika 10.1.0 changed reconnect semantics** ("stops automatic channel restoration when the connection closes", #717; "requires aiormq consumer publishing error propagation", #716). Robust reconnect still restores topology, but verify the stage template's `async for` consumer loop survives a `docker compose restart rabbitmq` in the e2e suite rather than assuming it. One restart test covers both this and the `fail_fast`/`reconnect_interval` settings.
- `connect_robust` has `fail_fast=True` by default: the first connection attempt failing raises instead of retrying. In Compose the broker may not be ready; either use `depends_on` with `condition: service_healthy` on the RabbitMQ healthcheck (preferred) or set `fail_fast=False` / wrap startup in a small retry.
- **FastStream** is the alternative to hand-rolled aio-pika. It has `AckPolicy.ACK_FIRST` (ack before handler) and `prefetch_count` on the channel, and a FastAPI router integration (**Official** FastStream docs). It would shorten the stage template but adds a framework, its own DI, and lifecycle on top of FastAPI's, and hides the channel/ack details the reviewer is meant to see. For an interview deliverable where orchestration is the evaluated part, plain aio-pika with ~40 lines of consumer loop is the smaller, more legible choice. Dramatiq/Celery are job frameworks that own retries themselves; they conflict with the orchestrator-owns-retries decision. Kombu is sync.

## 9. Versions pulled while researching (PyPI, 2026-10-13)

| Package | Latest | Python | Note |
|---|---|---|---|
| aio-pika | 10.1.1 (2026-10-10) | >=3.11, 3.14 classifier | 10.x is a major bump (aiormq 7) with reconnect-behaviour changes in 10.1.0 landed within the last three weeks. Latest 9.x is 9.6.2 (March). Pin `aio-pika>=10.1.1,<11`. The broker-restart e2e test (section 8) is a required deliverable, not optional: it is the only thing that validates this pin. Fall back to `9.6.2` only if it fails. |
| faststream | 0.7.7 (2026-09-23) | >=3.10, 3.14 | Only if chosen over aio-pika. |
| asyncpg | 0.32.0 (2026-10-06) | >=3.9, 3.14 | |
| testcontainers | 4.15.0 (2026-07-24) | >=3.10, 3.14 | `DockerCompose` module. |
| fastapi | 0.143.0 (2026-10-08) | >=3.10, 3.14 | |
| pytest-asyncio | 1.4.0 (2026-05-26) | >=3.10, 3.14 | |

Image tags (Postgres, RabbitMQ, uv base image) were not researched here.

## Sources

- RabbitMQ consumer acknowledgements and publisher confirms: https://www.rabbitmq.com/docs/confirms
- RabbitMQ consumers guide (delivery acknowledgement timeout, prefetch, SAC): https://www.rabbitmq.com/docs/consumers
- RabbitMQ reliability guide (idempotent consumers, redelivered flag, heartbeats): https://www.rabbitmq.com/docs/reliability
- RabbitMQ quorum queues (delivery-limit default 20, poison messages, classic default): https://www.rabbitmq.com/docs/quorum-queues
- RabbitMQ dead lettering triggers and x-death: https://www.rabbitmq.com/docs/dlx
- RabbitMQ TTL (expiry only at queue head): https://www.rabbitmq.com/docs/ttl
- RabbitMQ heartbeats: https://www.rabbitmq.com/docs/heartbeats
- aio-pika docs (process(), set_qos, publisher_confirms default, Pool, connect_robust fail_fast): https://github.com/mosquito/aio-pika/tree/master/docs/source and https://aio-pika.readthedocs.io
- aio-pika releases: https://github.com/mosquito/aio-pika/releases
- FastStream RabbitMQ ack policies: https://github.com/ag2ai/faststream/blob/main/docs/docs/en/rabbit/ack.md
- FastAPI lifespan: https://fastapi.tiangolo.com/advanced/events/
- testcontainers-python Compose module: https://github.com/testcontainers/testcontainers-python/blob/main/docs/features/docker_compose.md
- Outbox pattern (community): https://james-carr.org/posts/2026-01-15-transactional-outbox-pattern/ , https://www.conduktor.io/glossary/outbox-pattern-for-reliable-event-publishing
- SKIP LOCKED vs advisory locks (community): https://milanjovanovic.tech/blog/pessimistic-locking-ef-core-postgresql , https://www.2ndquadrant.com/en/?p=9279
- consumer_timeout behaviour (community): https://www.netdata.cloud/guides/rabbitmq/rabbitmq-consumer-timeout/
- pika heartbeat starvation by blocking callbacks (community): https://github.com/fedora-infra/fedora-messaging/issues/130
