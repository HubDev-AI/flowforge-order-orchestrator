# aio-pika 10.1.1 planning notes

Sources: Context7 (/mosquito/aio-pika: README, tutorials, robust_*.py, message.py, exchange.py), GitHub releases page (10.0.0 - 10.1.1). Items marked [broker] are RabbitMQ semantics from general knowledge, not fetched this session; items marked [unverified] were not confirmed in docs.

## 1. Summary / version
aio-pika 10.x: asyncio wrapper over aiormq 7 (10.0.0, 9 Jul 2026). Python 3.11+. 3.14 not explicitly confirmed in fetched material [unverified]; run the suite on 3.14 early.

Release trail (day/month from releases page, year implied 2026):
- 10.0.0 (9 Jul): aiormq 7.
- 10.0.1: connect/connect_robust typed via TypeVar.
- 10.0.2 (26 Sep): `ready()` on robust channel typed; `QueueIterator.__aenter__` async; publish() return annotated.
- 10.0.3: processing errors preserved when delivery channel closes.
- 10.0.4: fixes reconnect when topology depends on another channel; documents/tests publication failure across reconnects; timed-out iterators cleaned without reviving consumers.
- 10.1.0 (27 Sep): aiormq consumer publish errors propagate; channel auto-restoration STOPS when connection is closed (explicit close no longer resurrects channels); URL + connection kwargs now both apply.
- 10.1.1 (10 Oct): "Restore robust consumers after broker cancellation" (basic.cancel from broker, e.g. queue deleted/node failover, now re-consumed).
9.x to 10: 9.5.0 made QueueIterator raise StopAsyncIteration on channel close; in 10.x `RobustQueue.iterator()` returns `RobustQueueIterator` which waits for channel restoration instead (RETRY_DELAY 0.5s). Net: on 10.x, `async for` over a robust iterator survives reconnects; on 9.x it silently ended and consumers had to be re-armed. The 10.1.1 pin is the first release with the broker-cancel fix; do not pin lower.

No open issue found about consumers not resuming on 10.1.x (search returned nothing relevant; GitHub issues not browsed directly). Treat as untested: add an e2e test that restarts the rabbitmq container and asserts a stage still consumes.

## 2. Reconnect semantics (connect_robust)
Restored after reconnect: channels, QoS (channel-level, re-applied), exchanges and queues declared with `robust=True` (default) incl. bindings, and active consumers started via `queue.consume`/`queue.iterator`. Not restored: queues/exchanges declared `robust=False`, internal exchanges.
Not restored / caveats:
- In-flight unacked messages: the old channel is dead. `IncomingMessage.ack/reject/nack` raises `ChannelInvalidStateError` for a message received before the drop; broker redelivers (redelivered=True). Handler must tolerate duplicate delivery and catch that error on ack.
- Publications are NOT replayed. Docs on `Exchange.publish`: app decides whether to wait for `channel.ready()` and retry; a lost confirmation does not mean the broker rejected, retry can duplicate.
- Channel created during/after explicit `connection.close()` is not revived (10.1.0).

## 3. Publisher confirms
`connection.channel(publisher_confirms=True)` is the default. `await exchange.publish(msg, routing_key=..., timeout=...)` returns the confirmation (`aiormq.abc.ConfirmationFrameType`, a `Basic.Ack`) and raises `aio_pika.exceptions.DeliveryError` on nack (and on a mandatory return), `asyncio.TimeoutError`/`TimeoutError` on no confirm. Pattern (from tutorial 7):
```python
try:
    conf = await exchange.publish(Message(body, delivery_mode=DeliveryMode.PERSISTENT), routing_key=q, timeout=5.0, mandatory=True)
except (DeliveryError, TimeoutError): ...  # treat as "not dispatched"; sweep re-dispatches
```
`mandatory=True`: unroutable message is returned by broker (basic.return); with confirms on, aio-pika surfaces it as DeliveryError. Use it: publishing to the default exchange with a stage queue name that was never declared silently drops otherwise. Recommended: orchestrator declares all stage queues at startup, publishes via default exchange with routing_key=queue name, mandatory=True. Confirm means broker accepted (for durable+persistent: written/queued, [broker] quorum/classic classic-queue confirm after persist), not that a consumer got it.

## 4. Consumer patterns
- `queue.iterator()` (async-for in one task you own): back-pressure natural, cancellation is `task.cancel()`, exiting `async with` cancels the consumer. Preferred for the stage template: one loop task, explicit control.
- `queue.consume(callback)`: callback runs as a new task per delivery, up to prefetch_count concurrent; returns consumer tag; cancel with `queue.cancel(tag)`. Simpler for concurrent handlers; exceptions in callback are logged, not propagated.
- `async with message.process(requeue=False, reject_on_redelivered=False, ignore_processed=False)`: ack on clean exit; on exception reject (requeue=False default False? Docs: tutorial says exception returns message to queue, i.e. requeue=True is the context-manager default in tutorial 2 text; pass `requeue=False` explicitly and verify). `ignore_processed=True` lets you call `message.ack()/reject()` manually inside the block without double-ack error. Use manual control: `async with message.process(requeue=False, ignore_processed=True)`.
- `no_ack=True`: auto-ack on delivery, messages lost if consumer dies mid-handler. Do not use for stage commands.
- Prefetch: `await channel.set_qos(prefetch_count=N)` per channel, applies to consumers on it. N=1 for slow stages gives fair dispatch; raise for throughput.
- Ack timing (open question 3): ack after handler plus result publish (at-least-once; redelivery on crash). Because orchestrator owns attempts and a deadline sweep, a nack/requeue=False after a handler error plus an explicit failure result message is cleaner than broker requeue (avoids redelivery loop counting against nothing). Dedupe on (order id, stage, attempt) in orchestrator.

## 5. Dead-letter / TTL
```python
await channel.declare_exchange("dlx", ExchangeType.DIRECT, durable=True)
dlq = await channel.declare_queue("stage.inventory.dlq", durable=True); await dlq.bind("dlx", "stage.inventory")
q = await channel.declare_queue("stage.inventory", durable=True,
    arguments={"x-dead-letter-exchange": "dlx", "x-dead-letter-routing-key": "stage.inventory", "x-message-ttl": 60000})
msg = Message(body, expiration=timedelta(seconds=30))  # per-message TTL
```
[broker] Expired message with no DLX is silently dropped. With DLX it is routed there with x-death header. Per-message TTL is only evaluated at queue head for classic queues (expired message behind a live one lingers; consumer still would not receive it once at head). If both queue and message TTL set, the lower wins. Queue arguments are immutable: redeclare with different args raises PRECONDITION_FAILED (channel closes) - fix args up front; a robust redeclare after reconnect uses same args so is fine. Also: rejection with requeue=False dead-letters to DLX. Fit with the plan: command already carries its own deadline and stage template discards expired commands; broker TTL is optional hardening. Skipping broker TTL is simplest; if used, DLQ is only for forensics because the sweep re-dispatches from DB.

## 6. Heartbeat
Heartbeat is driven by aiormq on the same event loop (task-based). Async handlers that `await` never starve it. A blocking sync call or CPU loop (time.sleep, sync DB driver, big JSON) freezes the loop and the heartbeat; broker closes connection after about 2 missed intervals (default URL heartbeat 60s, so ~120s). Stage simulation must use `asyncio.sleep`, not `time.sleep`. Recommended: leave heartbeat at 30-60s (`?heartbeat=30`); handlers need no separate task unless they block, in which case `asyncio.to_thread`. Also: RabbitMQ consumer_timeout (default 30 min delivery ack timeout) closes channel if a delivery stays unacked too long; irrelevant unless handlers exceed it [broker].

## 7. Shutdown / topology guidance
- One connection per process, a channel per role/task: separate channels for publishing (confirms on) and consuming (QoS). Avoid sharing a channel across concurrent publishers needing confirms if throughput matters; for this scale one publish channel is fine.
- FastAPI lifespan: create connection in startup, store on `app.state`; on exit: cancel consumer/sweep tasks, `await asyncio.gather(*tasks, return_exceptions=True)`, then `await connection.close()` (closes channels; 10.1.0 no longer revives them). Cancel consumer first (`async with queue.iterator()` exit or `queue.cancel(tag)`), let in-flight handler finish or its message redelivers. With `queue.consume`, `await queue.cancel(tag)` stops new deliveries.
- Consumer task crash handling: wrap loop with try/except and log; an unhandled exception in a task silently kills consumption, add done-callback that logs/exits the process so Compose restarts it.
- Startup ordering: `connect_robust` retries initial connection? It does not loop forever by default for the first connect (raises if broker down at start); wrap in retry (tenacity or compose `depends_on` healthcheck on rabbitmq) [unverified].
- Reply queue (orchestrator results): durable named queue `orchestrator.results`, not exclusive/auto-delete, so results survive an orchestrator restart.

## 8. Decision-relevant items
1. 10.1.1 includes robust-consumer restore after broker cancel and iterator that survives reconnects: use `queue.iterator()`/`consume` on robust channels, no hand re-arm. Verify with a rabbitmq-restart e2e test.
2. Publish is not replayed across reconnect; confirms + mandatory + timeout give a detectable "not dispatched", so the sweep (not an outbox) is sufficient for dual-write recovery, with duplicate-tolerant handlers. Confirms: yes, keep default.
3. Ack after handler and result publish; `ChannelInvalidStateError` on stale ack means redelivery, so stage handler idempotency per (order, stage) is mandatory (already settled).
4. Heartbeat safe with async handlers; do not use blocking sleeps in failure/latency injection.
5. Context-manager default requeue behavior should be pinned in code (`requeue=False`) and tested; docs conflict on default.

## References
- https://github.com/mosquito/aio-pika/releases
- https://github.com/mosquito/aio-pika/blob/master/aio_pika/robust_queue.py, robust_channel.py, message.py, exchange.py
- https://github.com/mosquito/aio-pika/blob/master/docs/source/rabbitmq-tutorial/7-publisher-confirms.md
- https://aio-pika.readthedocs.io/
