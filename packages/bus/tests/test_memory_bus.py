import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bus import MemoryBus, UnroutableError
from contracts import Command, CommandKind, Outcome, Result

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def command(stage="fraud", deadline_in=10, attempt=1):
    return Command(
        order_id=uuid4(),
        stage=stage,
        attempt=attempt,
        kind=CommandKind.EXECUTE,
        deadline_at=NOW + timedelta(seconds=deadline_in),
        payload={"sku": "A-1"},
    )


@pytest.fixture
async def bus():
    bus = MemoryBus(clock=lambda: NOW)
    await bus.declare_topology(["inventory", "fraud"])
    return bus


async def next_item(iterator, wait=1):
    async with asyncio.timeout(wait):
        return await anext(iterator)


async def test_command_reaches_the_consumer_of_its_stage(bus):
    sent = command()
    await bus.publish_command(sent)

    assert await next_item(bus.consume_commands("fraud")) == sent


async def test_result_reaches_the_results_consumer(bus):
    sent = Result(
        order_id=uuid4(),
        stage="fraud",
        attempt=1,
        kind=CommandKind.EXECUTE,
        outcome=Outcome.SUCCEEDED,
    )
    await bus.publish_result(sent)

    assert await next_item(bus.consume_results()) == sent


async def test_command_published_before_any_consumer_waits_in_the_queue(bus):
    sent = command()

    await bus.publish_command(sent)

    assert await next_item(bus.consume_commands("fraud")) == sent


async def test_publishing_to_an_undeclared_stage_raises(bus):
    with pytest.raises(UnroutableError, match="shipping"):
        await bus.publish_command(command(stage="shipping"))


async def test_expired_commands_are_dropped_and_the_live_one_is_delivered(bus):
    live = command(deadline_in=10, attempt=3)
    await bus.publish_command(command(deadline_in=-1, attempt=1))
    await bus.publish_command(command(deadline_in=-5, attempt=2))
    await bus.publish_command(live)
    consumer = bus.consume_commands("fraud")

    assert await next_item(consumer) == live
    with pytest.raises(TimeoutError):
        await next_item(consumer, wait=0.05)


async def test_a_failing_consumer_does_not_cause_redelivery(bus):
    first, second = command(attempt=1), command(attempt=2)
    await bus.publish_command(first)
    await bus.publish_command(second)

    with pytest.raises(RuntimeError):
        async for _ in bus.consume_commands("fraud"):
            raise RuntimeError("handler blew up")

    assert await next_item(bus.consume_commands("fraud")) == second
