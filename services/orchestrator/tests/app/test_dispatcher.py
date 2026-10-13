import logging
from datetime import timedelta
from uuid import UUID

from bus import MemoryBus
from contracts import CommandKind, Outcome
from orchestrator.app.dispatcher import Dispatcher


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


async def test_a_tick_with_nothing_to_do_changes_nothing(dispatcher):
    await dispatcher.tick()


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


async def test_a_failed_publish_is_logged_and_recovered_by_the_deadline(clock, client_for, caplog):
    class FlakyBus(MemoryBus):
        failures = 1

        async def publish_command(self, command):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("broker unreachable")
            await super().publish_command(command)

    bus = FlakyBus(clock=clock)
    runtime, submit = await client_for(bus)
    await submit()
    dispatcher = Dispatcher(runtime.store, bus, runtime.config, clock, runtime.nudge)

    with caplog.at_level(logging.ERROR):
        await dispatcher.tick()
    assert "broker unreachable" in caplog.text

    clock.advance(11)
    await dispatcher.tick()
    clock.advance(2)
    await dispatcher.tick()

    consumer = bus.consume_commands("inventory")
    command = await anext(consumer)
    await consumer.aclose()
    assert command.attempt == 2


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
