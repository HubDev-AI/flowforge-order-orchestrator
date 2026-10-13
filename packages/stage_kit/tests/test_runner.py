import asyncio
import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from bus import MemoryBus
from contracts import Command, CommandKind, Outcome
from stage_kit.handler import StageHandler
from stage_kit.runner import StageRunner
from stage_kit.simulator import Profile, Simulator

QUIET = Profile(latency_min_s=0, latency_max_s=0, transient_probability=0, reject_probability=0)


class RecordingHandler(StageHandler):
    def __init__(self, output=None, error=None):
        self.output, self.error, self.executed, self.undone = output, error, [], []

    async def execute(self, command):
        self.executed.append(command)
        if self.error:
            raise self.error
        return self.output


class UndoHandler(RecordingHandler):
    async def undo(self, command):
        self.undone.append(command)
        return {"released": command.prior_stage_output}


def command(attempt=1, kind=CommandKind.EXECUTE, script=None, prior=None):
    return Command(
        order_id=uuid4(),
        stage="fraud",
        attempt=attempt,
        kind=kind,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
        payload={"sku": "A-1"},
        script=script or {},
        prior_stage_output=prior,
    )


def runner(handler, bus, profile=QUIET):
    return StageRunner("fraud", handler, Simulator(profile, random.Random(1)), bus)


async def make_bus():
    bus = MemoryBus()
    await bus.declare_topology(["fraud"])
    return bus


async def test_a_successful_stage_reports_the_handler_output_with_the_command_identifiers():
    handler = RecordingHandler(output={"score": 0.1})
    sent = command(attempt=2)

    result = await runner(handler, await make_bus()).process(sent)

    assert (result.order_id, result.stage, result.attempt, result.kind) == (
        sent.order_id,
        "fraud",
        2,
        CommandKind.EXECUTE,
    )
    assert (result.outcome, result.stage_output) == (Outcome.SUCCEEDED, {"score": 0.1})
    assert handler.executed == [sent]


async def test_a_simulated_transient_failure_skips_the_handler():
    handler = RecordingHandler()
    sent = command(script={"fraud": {"execute": ["retry"]}})

    result = await runner(handler, await make_bus()).process(sent)

    assert result.outcome is Outcome.RETRY
    assert handler.executed == []


async def test_a_simulated_rejection_skips_the_handler_and_says_so():
    handler = RecordingHandler()
    sent = command(script={"fraud": {"execute": ["reject"]}})

    result = await runner(handler, await make_bus()).process(sent)

    assert (result.outcome, handler.executed) == (Outcome.REJECTED, [])
    assert result.reason


async def test_a_handler_exception_becomes_a_retry_named_by_its_class():
    handler = RecordingHandler(error=ValueError("secret detail"))

    result = await runner(handler, await make_bus()).process(command())

    assert (result.outcome, result.reason) == (Outcome.RETRY, "ValueError")


async def test_an_undo_command_reaches_the_handler_with_the_execute_output():
    handler = UndoHandler()
    sent = command(kind=CommandKind.UNDO, prior={"reservation_id": "r-1"})

    result = await runner(handler, await make_bus()).process(sent)

    assert handler.undone == [sent]
    assert handler.executed == []
    assert (result.outcome, result.stage_output) == (
        Outcome.SUCCEEDED,
        {"released": {"reservation_id": "r-1"}},
    )


async def test_an_undo_without_an_override_succeeds_and_calls_nothing():
    handler = RecordingHandler()

    result = await runner(handler, await make_bus()).process(
        command(kind=CommandKind.UNDO, prior={"reservation_id": "r-1"})
    )

    assert (result.outcome, result.stage_output) == (Outcome.SUCCEEDED, None)
    assert handler.executed == []


async def test_the_same_command_delivered_twice_calls_the_handler_twice_with_one_identity():
    handler = RecordingHandler(output={"score": 0.1})
    bus = await make_bus()
    sent = command()
    stage = runner(handler, bus)

    await stage.process(sent)
    await stage.process(sent)

    assert [(c.order_id, c.stage, c.attempt) for c in handler.executed] == [
        (sent.order_id, "fraud", 1)
    ] * 2


async def test_the_loop_publishes_a_result_for_every_command_it_consumes():
    handler = RecordingHandler(output={"score": 0.1})
    bus = await make_bus()
    stage = runner(handler, bus)
    sent = [command(attempt=n) for n in (1, 2)]
    for item in sent:
        await bus.publish_command(item)

    task = asyncio.create_task(stage.run())
    try:
        results = bus.consume_results()
        async with asyncio.timeout(2):
            received = [await anext(results), await anext(results)]
    finally:
        task.cancel()
        await results.aclose()

    assert [r.attempt for r in received] == [1, 2]
    assert all(r.outcome is Outcome.SUCCEEDED for r in received)
