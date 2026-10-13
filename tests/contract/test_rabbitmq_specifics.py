import asyncio
from contextlib import aclosing
from datetime import timedelta
from uuid import uuid4

import aio_pika
import pytest
from bus import RESULTS_QUEUE, RabbitMQBus, stage_queue
from contracts import Command, CommandKind, Outcome, Result

pytestmark = pytest.mark.contract


async def publish_raw(url: str, queue: str, body: bytes) -> None:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        await channel.default_exchange.publish(aio_pika.Message(body), routing_key=queue)


async def test_a_malformed_result_is_dropped_and_consumption_continues(
    rabbitmq_bus_factory, rabbitmq_container, amqp_url_of
):
    bus = await rabbitmq_bus_factory()
    valid = Result(
        order_id=uuid4(),
        stage="fraud",
        attempt=1,
        kind=CommandKind.EXECUTE,
        outcome=Outcome.SUCCEEDED,
    )
    await publish_raw(amqp_url_of(rabbitmq_container), RESULTS_QUEUE, b"not json")
    await bus.publish_result(valid)

    async with aclosing(bus.consume_results()) as consumer:
        async with asyncio.timeout(5):
            assert await anext(consumer) == valid
    await bus.close()


async def test_a_malformed_command_is_dropped_and_consumption_continues(
    rabbitmq_bus_factory, rabbitmq_container, amqp_url_of, now
):
    bus = await rabbitmq_bus_factory()
    valid = Command(
        order_id=uuid4(),
        stage="fraud",
        attempt=1,
        kind=CommandKind.EXECUTE,
        deadline_at=now + timedelta(seconds=10),
        payload={},
    )
    await publish_raw(amqp_url_of(rabbitmq_container), stage_queue("fraud"), b"{}")
    await bus.publish_command(valid)

    async with aclosing(bus.consume_commands("fraud")) as consumer:
        async with asyncio.timeout(5):
            assert await anext(consumer) == valid
    await bus.close()


async def test_publishing_and_consuming_resume_after_the_broker_restarts(
    new_rabbitmq, free_port, amqp_url_of, now
):
    container = new_rabbitmq(free_port())
    with container:
        bus = await RabbitMQBus.connect(amqp_url_of(container), clock=lambda: now)
        await bus.declare_topology(["fraud"])

        def command(attempt: int) -> Command:
            return Command(
                order_id=uuid4(),
                stage="fraud",
                attempt=attempt,
                kind=CommandKind.EXECUTE,
                deadline_at=now + timedelta(seconds=60),
                payload={},
            )

        before, after = command(1), command(2)
        await bus.publish_command(before)
        container.get_wrapped_container().restart()

        async with asyncio.timeout(60):
            while True:
                try:
                    await bus.publish_command(after)
                    break
                except Exception:
                    await asyncio.sleep(0.5)

        async with aclosing(bus.consume_commands("fraud")) as consumer:
            async with asyncio.timeout(60):
                received = [await anext(consumer), await anext(consumer)]
        await bus.close()

    assert received == [before, after]


async def test_an_unfinished_result_is_redelivered_to_the_next_consumer(rabbitmq_bus_factory):
    bus = await rabbitmq_bus_factory()
    results = [
        Result(
            order_id=uuid4(),
            stage="fraud",
            attempt=number,
            kind=CommandKind.EXECUTE,
            outcome=Outcome.SUCCEEDED,
        )
        for number in (1, 2)
    ]
    for result in results:
        await bus.publish_result(result)

    async with aclosing(bus.consume_results()) as consumer:
        async with asyncio.timeout(5):
            received = [await anext(consumer), await anext(consumer)]
    async with aclosing(bus.consume_results()) as consumer:
        async with asyncio.timeout(5):
            redelivered = await anext(consumer)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await anext(consumer)
    await bus.close()

    assert received == results
    assert redelivered == results[1]


async def test_a_consumer_attached_before_a_broker_restart_keeps_receiving(
    new_rabbitmq, free_port, amqp_url_of, now
):
    container = new_rabbitmq(free_port())
    with container:
        bus = await RabbitMQBus.connect(amqp_url_of(container), clock=lambda: now)
        await bus.declare_topology(["fraud"])
        consumer = bus.consume_commands("fraud")
        waiting = asyncio.ensure_future(anext(consumer))
        await asyncio.sleep(1)
        container.get_wrapped_container().restart()
        sent = Command(
            order_id=uuid4(),
            stage="fraud",
            attempt=1,
            kind=CommandKind.EXECUTE,
            deadline_at=now + timedelta(seconds=60),
            payload={},
        )

        async with asyncio.timeout(60):
            while True:
                try:
                    await bus.publish_command(sent)
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            received = await waiting
        await consumer.aclose()
        await bus.close()

    assert received == sent
