import asyncio
import logging
from contextlib import aclosing
from datetime import timedelta
from uuid import UUID

from bus import MemoryBus
from contracts import CommandKind, Outcome
from orchestrator.app.dispatcher import CLAIM_BATCH_SIZE, Dispatcher
from orchestrator.domain.models import OrderState


async def test_a_due_order_is_dispatched_by_one_tick(
    submit, dispatcher, next_command, status, clock
):
    order = (await submit(script={"fraud": {"execute": ["reject"]}})).json()

    await dispatcher.tick()

    command = await next_command("inventory")
    assert (command.order_id, command.attempt, command.kind) == (
        UUID(order["id"]),
        1,
        CommandKind.EXECUTE,
    )
    assert command.deadline_at == clock.now + timedelta(seconds=10)
    assert command.payload == {"sku": "A-1"}
    assert command.script.root["fraud"][CommandKind.EXECUTE][0].value == "reject"
    assert (await status(order["id"]))["state"] == "running"


async def test_an_order_is_not_dispatched_before_the_pending_threshold_passes(
    submit, clock, runtime, next_command, status
):
    runtime.config = runtime.config.model_copy(
        update={
            "sweep": runtime.config.sweep.model_copy(update={"pending_threshold_seconds": 30.0})
        }
    )
    dispatcher = Dispatcher(runtime.store, runtime.bus, runtime.config, clock, runtime.nudge)
    order = (await submit()).json()

    await dispatcher.tick()
    assert (await status(order["id"]))["state"] == "pending"

    clock.advance(31)
    await dispatcher.tick()
    assert (await status(order["id"]))["state"] == "running"
    assert (await next_command("inventory")).attempt == 1


async def test_a_retry_waits_for_its_backoff_before_the_next_attempt(
    submit, dispatcher, results, next_command, reply, clock, history
):
    order = (await submit()).json()
    await dispatcher.tick()
    await results.handle(reply(await next_command("inventory"), Outcome.RETRY, reason="flaky"))

    await dispatcher.tick()
    assert await history(order["id"]) == [("inventory", "execute", 1, "retry")]

    clock.advance(1)
    await dispatcher.tick()
    assert (await next_command("inventory")).attempt == 2
    assert (await history(order["id"]))[-1] == ("inventory", "execute", 2, None)


async def test_an_attempt_past_its_deadline_is_timed_out_and_its_late_result_is_dropped(
    submit, dispatcher, results, next_command, reply, clock, history, status
):
    order = (await submit()).json()
    await dispatcher.tick()
    first = await next_command("inventory")

    clock.advance(11)
    await dispatcher.tick()
    assert await history(order["id"]) == [("inventory", "execute", 1, "timed_out")]

    clock.advance(2)
    await dispatcher.tick()
    second = await next_command("inventory")
    assert second.attempt == 2

    assert await results.handle(reply(first)) is False
    assert await history(order["id"]) == [
        ("inventory", "execute", 1, "timed_out"),
        ("inventory", "execute", 2, None),
    ]
    assert await results.handle(reply(second)) is True
    assert (await status(order["id"]))["current_stage"] == "fraud"


class FlakyBus(MemoryBus):
    def __init__(self, clock, failures: int) -> None:
        super().__init__(clock=clock)
        self.failures = failures

    async def publish_command(self, command):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("broker unreachable")
        await super().publish_command(command)


async def test_a_failed_publish_releases_the_attempt_and_the_order_is_retried_without_cost(
    clock, client_for, caplog
):
    bus = FlakyBus(clock, failures=1)
    runtime, submit = await client_for(bus)
    order = (await submit()).json()
    dispatcher = Dispatcher(runtime.store, bus, runtime.config, clock, runtime.nudge)

    with caplog.at_level(logging.ERROR):
        await dispatcher.tick()
    assert "broker unreachable" in caplog.text
    assert await runtime.store.list_attempts(UUID(order["id"])) == []
    assert (await runtime.store.get_order(UUID(order["id"]))).next_due_at == clock.now + timedelta(
        seconds=runtime.config.sweep.interval_seconds
    )

    await dispatcher.tick()
    assert await runtime.store.list_attempts(UUID(order["id"])) == []
    clock.advance(runtime.config.sweep.interval_seconds)
    await dispatcher.tick()

    async with aclosing(bus.consume_commands("inventory")) as consumer:
        command = await anext(consumer)
    assert command.attempt == 1
    assert [a.attempt for a in await runtime.store.list_attempts(UUID(order["id"]))] == [1]


async def test_a_broker_outage_does_not_consume_the_attempt_cap(clock, client_for):
    bus = FlakyBus(clock, failures=10**6)
    runtime, submit = await client_for(bus)
    order = (await submit()).json()
    dispatcher = Dispatcher(runtime.store, bus, runtime.config, clock, runtime.nudge)

    for _ in range(120):
        await dispatcher.tick()
        clock.advance(1)

    stored = await runtime.store.get_order(UUID(order["id"]))
    assert stored.state is OrderState.RUNNING
    assert await runtime.store.list_attempts(UUID(order["id"])) == []


async def test_the_first_failed_publish_releases_the_rest_of_the_batch(clock, client_for):
    bus = FlakyBus(clock, failures=0)
    runtime, submit = await client_for(bus)
    orders = [(await submit(key=f"k{n}")).json() for n in range(3)]
    dispatcher = Dispatcher(runtime.store, bus, runtime.config, clock, runtime.nudge)
    published = []
    original = bus.publish_command

    async def publish_then_fail(command):
        if published:
            raise RuntimeError("broker went away")
        published.append(command.order_id)
        await original(command)

    bus.publish_command = publish_then_fail
    await dispatcher.tick()

    open_attempts = [len(await runtime.store.list_attempts(UUID(order["id"]))) for order in orders]
    assert open_attempts == [1, 0, 0]
    assert published == [UUID(orders[0]["id"])]


async def test_a_full_batch_is_followed_by_another_tick_without_waiting(clock, client_for):
    bus = MemoryBus(clock=clock)
    runtime, submit = await client_for(bus)
    ids = [UUID((await submit(key=f"k{n}")).json()["id"]) for n in range(CLAIM_BATCH_SIZE + 1)]
    slow = runtime.config.model_copy(
        update={"sweep": runtime.config.sweep.model_copy(update={"interval_seconds": 60.0})}
    )
    dispatcher = Dispatcher(runtime.store, bus, slow, clock, runtime.nudge)
    task = asyncio.create_task(dispatcher.run())

    async def all_dispatched():
        counts = [len(await runtime.store.list_attempts(order_id)) for order_id in ids]
        return counts == [1] * len(ids)

    try:
        async with asyncio.timeout(2):
            while not await all_dispatched():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()


async def test_an_undo_command_carries_the_output_of_the_execute_it_reverses(
    submit, dispatcher, results, next_command, reply
):
    await submit()
    await dispatcher.tick()
    await results.handle(
        reply(await next_command("inventory"), stage_output={"reservation_id": "r-1"})
    )
    await dispatcher.tick()
    await results.handle(reply(await next_command("fraud"), Outcome.REJECTED, reason="risky"))
    await dispatcher.tick()
    fraud_undo = await next_command("fraud")
    await results.handle(reply(fraud_undo))
    await dispatcher.tick()

    inventory_undo = await next_command("inventory")

    assert fraud_undo.kind is CommandKind.UNDO
    assert fraud_undo.prior_stage_output is None
    assert inventory_undo.kind is CommandKind.UNDO
    assert inventory_undo.prior_stage_output == {"reservation_id": "r-1"}
