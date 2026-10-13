import asyncio
from contextlib import aclosing
from datetime import timedelta
from uuid import uuid4

import pytest
from bus import UnroutableError
from contracts import Command, CommandKind, Outcome, Result


@pytest.fixture
def command(now):
    def build(stage="fraud", deadline_in=10, attempt=1):
        return Command(
            order_id=uuid4(),
            stage=stage,
            attempt=attempt,
            kind=CommandKind.EXECUTE,
            deadline_at=now + timedelta(seconds=deadline_in),
            payload={"sku": "A-1"},
        )

    return build


async def next_item(consumer, wait=5):
    async with asyncio.timeout(wait):
        return await anext(consumer)


async def test_command_reaches_the_consumer_of_its_stage(bus, command):
    sent = command()
    await bus.publish_command(sent)

    async with aclosing(bus.consume_commands("fraud")) as consumer:
        assert await next_item(consumer) == sent


async def test_result_reaches_the_results_consumer(bus):
    sent = Result(
        order_id=uuid4(),
        stage="fraud",
        attempt=1,
        kind=CommandKind.EXECUTE,
        outcome=Outcome.SUCCEEDED,
    )
    await bus.publish_result(sent)

    async with aclosing(bus.consume_results()) as consumer:
        assert await next_item(consumer) == sent


async def test_command_published_before_any_consumer_waits_in_the_queue(bus, command):
    first, second = command(attempt=1), command(attempt=2)
    await bus.publish_command(first)
    await bus.publish_command(second)

    async with aclosing(bus.consume_commands("fraud")) as consumer:
        assert [await next_item(consumer), await next_item(consumer)] == [first, second]


async def test_commands_of_other_stages_stay_in_their_own_queue(bus, command):
    await bus.publish_command(command(stage="inventory"))
    live = command(stage="fraud")
    await bus.publish_command(live)

    async with aclosing(bus.consume_commands("fraud")) as consumer:
        assert await next_item(consumer) == live


async def test_publishing_to_an_undeclared_stage_raises(bus, command):
    with pytest.raises(UnroutableError, match="shipping"):
        await bus.publish_command(command(stage="shipping"))


async def test_expired_commands_are_dropped_and_the_live_one_is_delivered(bus, command):
    live = command(deadline_in=10, attempt=3)
    await bus.publish_command(command(deadline_in=-1, attempt=1))
    await bus.publish_command(command(deadline_in=-5, attempt=2))
    await bus.publish_command(live)

    async with aclosing(bus.consume_commands("fraud")) as consumer:
        assert await next_item(consumer) == live
        with pytest.raises(TimeoutError):
            await next_item(consumer, wait=0.3)


async def test_a_failing_consumer_does_not_cause_redelivery(bus, command):
    first, second = command(attempt=1), command(attempt=2)
    await bus.publish_command(first)
    await bus.publish_command(second)

    with pytest.raises(RuntimeError):
        async with aclosing(bus.consume_commands("fraud")) as consumer:
            async for _ in consumer:
                raise RuntimeError("handler blew up")

    async with aclosing(bus.consume_commands("fraud")) as consumer:
        assert await next_item(consumer) == second
