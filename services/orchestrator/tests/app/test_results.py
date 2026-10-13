import asyncio
from uuid import uuid4

from contracts import CommandKind, Outcome, Result
from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.app.dispatcher import Dispatcher
from orchestrator.app.results import NO_REASON, ResultsConsumer

from .conftest import pipeline_config


async def run_through(stages, submit, dispatcher, results, next_command, reply):
    order = (await submit()).json()
    for stage in stages:
        await dispatcher.tick()
        assert await results.handle(reply(await next_command(stage))) is True
    return order


async def test_an_order_runs_every_stage_and_completes(
    submit, dispatcher, results, next_command, reply, status, history
):
    order = await run_through(
        ["inventory", "fraud", "shipping"], submit, dispatcher, results, next_command, reply
    )

    assert (await status(order["id"]))["state"] == "completed"
    assert await history(order["id"]) == [
        ("inventory", "execute", 1, "succeeded"),
        ("fraud", "execute", 1, "succeeded"),
        ("shipping", "execute", 1, "succeeded"),
    ]


async def test_a_settled_result_wakes_the_dispatcher_and_a_stale_one_does_not(
    submit, dispatcher, results, next_command, reply, runtime
):
    await submit()
    await dispatcher.tick()
    result = reply(await next_command("inventory"))
    runtime.nudge.clear()

    await results.handle(result)
    assert runtime.nudge.is_set()
    runtime.nudge.clear()
    await results.handle(result)

    assert not runtime.nudge.is_set()


async def test_a_result_after_its_deadline_counts_as_a_timeout_before_the_sweep_runs(
    submit, dispatcher, results, next_command, reply, clock, history, status
):
    order = (await submit()).json()
    await dispatcher.tick()
    command = await next_command("inventory")
    clock.advance(11)

    assert await results.handle(reply(command)) is True

    assert await history(order["id"]) == [("inventory", "execute", 1, "timed_out")]
    body = await status(order["id"])
    assert (body["state"], body["current_stage"]) == ("running", "inventory")
    assert body["next_attempt_due_at"] is not None


async def test_a_late_rejection_is_a_timeout_too_so_the_attempt_is_retried(
    submit, dispatcher, results, next_command, reply, clock, history, status
):
    order = (await submit()).json()
    await dispatcher.tick()
    command = await next_command("inventory")
    clock.advance(11)

    assert await results.handle(reply(command, Outcome.REJECTED, reason="too late")) is True

    assert await history(order["id"]) == [("inventory", "execute", 1, "timed_out")]
    assert (await status(order["id"]))["state"] == "running"


async def test_a_stage_output_is_kept_on_its_attempt(
    submit, dispatcher, results, next_command, reply, status
):
    order = (await submit()).json()
    await dispatcher.tick()

    await results.handle(
        reply(await next_command("inventory"), stage_output={"reservation_id": "r-1"})
    )

    attempt = (await status(order["id"]))["attempts"][0]
    assert attempt["stage_output"] == {"reservation_id": "r-1"}


async def test_the_same_result_delivered_twice_advances_the_order_once(
    submit, dispatcher, results, next_command, reply, history, status
):
    order = (await submit()).json()
    await dispatcher.tick()
    result = reply(await next_command("inventory"))

    first = await results.handle(result)
    second = await results.handle(result)

    assert (first, second) == (True, False)
    assert await history(order["id"]) == [("inventory", "execute", 1, "succeeded")]
    assert (await status(order["id"]))["current_stage"] == "fraud"


async def test_a_result_for_an_unknown_order_is_ignored(
    results, reply, next_command, submit, dispatcher
):
    await submit()
    await dispatcher.tick()
    command = await next_command("inventory")
    stray = reply(command).model_copy(update={"order_id": uuid4()})

    assert await results.handle(stray) is False


async def test_an_outcome_only_the_orchestrator_may_record_is_ignored(
    submit, dispatcher, results, next_command, reply, history
):
    order = (await submit()).json()
    await dispatcher.tick()
    command = await next_command("inventory")

    assert await results.handle(reply(command, Outcome.EXHAUSTED)) is False
    assert await results.handle(reply(command, Outcome.TIMED_OUT)) is False
    assert await history(order["id"]) == [("inventory", "execute", 1, None)]


async def test_a_retry_without_a_reason_is_recorded_with_a_placeholder(
    submit, dispatcher, results, next_command, reply, status
):
    order = (await submit()).json()
    await dispatcher.tick()

    await results.handle(reply(await next_command("inventory"), Outcome.RETRY))

    assert (await status(order["id"]))["attempts"][0]["reason"] == NO_REASON


async def test_a_rejection_undoes_the_stages_in_reverse_and_the_order_fails(
    submit, dispatcher, results, next_command, reply, history, status
):
    order = (await submit()).json()
    await dispatcher.tick()
    await results.handle(reply(await next_command("inventory")))
    await dispatcher.tick()
    await results.handle(reply(await next_command("fraud"), Outcome.REJECTED, reason="risky"))
    for stage in ("fraud", "inventory"):
        await dispatcher.tick()
        await results.handle(reply(await next_command(stage)))

    body = await status(order["id"])

    assert body["state"] == "failed"
    assert "fraud rejected: risky" in body["terminal_reason"]
    assert await history(order["id"]) == [
        ("inventory", "execute", 1, "succeeded"),
        ("fraud", "execute", 1, "rejected"),
        ("fraud", "undo", 1, "succeeded"),
        ("inventory", "undo", 1, "succeeded"),
    ]


async def test_an_undo_that_keeps_failing_ends_in_compensation_failed(
    submit, dispatcher, results, next_command, reply, status, clock
):
    order = (await submit()).json()
    await dispatcher.tick()
    await results.handle(reply(await next_command("inventory"), Outcome.REJECTED, reason="no"))
    for _ in range(3):
        await dispatcher.tick()
        await results.handle(reply(await next_command("inventory"), Outcome.RETRY, reason="stuck"))
        clock.advance(40)

    body = await status(order["id"])

    assert body["state"] == "compensation_failed"
    assert [a["outcome"] for a in body["attempts"]][-1] == "exhausted"


async def test_an_order_finishes_on_the_stage_list_it_was_submitted_with(
    submit, runtime, clock, next_command, reply, status
):
    first = (await submit(key="k1")).json()
    dispatcher = Dispatcher(runtime.store, runtime.bus, runtime.config, clock, runtime.nudge)
    await dispatcher.tick()

    smaller = pipeline_config(["inventory", "shipping"])
    runtime.config = smaller
    restarted = Dispatcher(runtime.store, runtime.bus, smaller, clock, runtime.nudge)
    results = ResultsConsumer(runtime.store, runtime.bus, smaller, clock, runtime.nudge)
    second = (await submit(key="k2")).json()

    await results.handle(reply(await next_command("inventory")))
    await restarted.tick()
    assert (await status(first["id"]))["current_stage"] == "fraud"
    await results.handle(reply(await next_command("fraud")))
    await restarted.tick()
    await results.handle(reply(await next_command("shipping")))
    assert (await status(first["id"]))["state"] == "completed"

    await restarted.tick()
    await results.handle(reply(await next_command("inventory")))
    await restarted.tick()
    assert (await status(second["id"]))["current_stage"] == "shipping"


async def test_an_undo_outstanding_across_a_restart_is_retried_and_the_order_fails(
    submit, runtime, clock, dispatcher, results, next_command, reply, status, history
):
    order = (await submit()).json()
    await dispatcher.tick()
    await results.handle(
        reply(await next_command("inventory"), stage_output={"reservation_id": "r-1"})
    )
    await dispatcher.tick()
    await results.handle(reply(await next_command("fraud"), Outcome.REJECTED, reason="risky"))
    await dispatcher.tick()
    await results.handle(reply(await next_command("fraud")))
    await dispatcher.tick()

    restarted = Dispatcher(runtime.store, runtime.bus, runtime.config, clock, runtime.nudge)
    resumed = ResultsConsumer(runtime.store, runtime.bus, runtime.config, clock, runtime.nudge)
    body = await status(order["id"])
    assert (body["state"], body["current_stage"]) == ("compensating", "inventory")

    clock.advance(11)
    await restarted.tick()
    clock.advance(2)
    await restarted.tick()
    retry = await next_command("inventory")
    await resumed.handle(reply(retry))

    assert retry.attempt == 2
    assert (await status(order["id"]))["state"] == "failed"
    assert (await history(order["id"]))[-2:] == [
        ("inventory", "undo", 1, "timed_out"),
        ("inventory", "undo", 2, "succeeded"),
    ]


async def test_the_consumer_loop_survives_a_handler_failure(runtime, clock, results, reply, caplog):
    class FlakyStore(MemoryStore):
        failures = 1

        async def settle(self, key, resolve):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("database down")
            return await super().settle(key, resolve)

    store = FlakyStore()
    consumer = ResultsConsumer(store, runtime.bus, runtime.config, clock, runtime.nudge)
    handled = []
    both_handled = asyncio.Event()
    original = consumer.handle

    async def recording(result):
        handled.append(result.attempt)
        try:
            return await original(result)
        finally:
            if len(handled) == 2:
                both_handled.set()

    consumer.handle = recording
    for attempt in (1, 2):
        await runtime.bus.publish_result(
            Result(
                order_id=uuid4(),
                stage="inventory",
                attempt=attempt,
                kind=CommandKind.EXECUTE,
                outcome=Outcome.SUCCEEDED,
            )
        )
    task = asyncio.create_task(consumer.run())
    try:
        async with asyncio.timeout(2):
            await both_handled.wait()
    finally:
        task.cancel()
    assert handled == [1, 2]
    assert "database down" in caplog.text
