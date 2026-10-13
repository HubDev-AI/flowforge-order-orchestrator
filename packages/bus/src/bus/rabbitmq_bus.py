import logging
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection
from aio_pika.exceptions import ChannelInvalidStateError, DeliveryError
from contracts import Command, Result, utc_now
from pydantic import BaseModel, ValidationError

from bus.port import RESULTS_QUEUE, NotPublished, UnroutableError, log_expired, stage_queue

logger = logging.getLogger(__name__)

PUBLISH_TIMEOUT_SECONDS = 5.0
PREFETCH_COUNT = 1
AMQP_NO_ROUTE = 312


class RabbitMQBus:
    def __init__(
        self,
        connection: AbstractRobustConnection,
        publisher: aio_pika.abc.AbstractRobustChannel,
        clock: Callable[[], datetime],
    ) -> None:
        self._connection = connection
        self._publisher = publisher
        self._clock = clock

    @classmethod
    async def connect(cls, url: str, clock: Callable[[], datetime] = utc_now) -> RabbitMQBus:
        connection = await aio_pika.connect_robust(url)
        publisher = await connection.channel(publisher_confirms=True, on_return_raises=True)
        return cls(connection, publisher, clock)

    async def declare_topology(self, stages: Sequence[str]) -> None:
        for name in (*map(stage_queue, stages), RESULTS_QUEUE):
            await self._publisher.declare_queue(name, durable=True)

    async def publish_command(self, command: Command) -> None:
        await self._publish(stage_queue(command.stage), command)

    async def publish_result(self, result: Result) -> None:
        await self._publish(RESULTS_QUEUE, result)

    async def consume_results(self) -> AsyncIterator[Result]:
        async with self._connection.channel() as channel:
            await channel.set_qos(prefetch_count=PREFETCH_COUNT)
            queue = await channel.declare_queue(RESULTS_QUEUE, durable=True)
            async with queue.iterator() as messages:
                async for message in messages:
                    result = _parse(Result, message)
                    if result is None:
                        await message.reject(requeue=False)
                        continue
                    yield result
                    await _ack(message)

    async def consume_commands(self, stage: str) -> AsyncIterator[Command]:
        async with self._connection.channel() as channel:
            await channel.set_qos(prefetch_count=PREFETCH_COUNT)
            queue = await channel.declare_queue(stage_queue(stage), durable=True)
            async with queue.iterator() as messages:
                async for message in messages:
                    if not await _ack(message):
                        continue
                    command = _parse(Command, message)
                    if command is None:
                        continue
                    if command.deadline_at > self._clock():
                        yield command
                    else:
                        log_expired(command)

    async def close(self) -> None:
        await self._connection.close()

    async def _publish(self, queue: str, message: BaseModel) -> None:
        if self._publisher.is_closed:
            raise NotPublished(f"publisher channel is closed; nothing sent to {queue}")
        body = aio_pika.Message(
            message.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        try:
            await self._publisher.default_exchange.publish(
                body, routing_key=queue, timeout=PUBLISH_TIMEOUT_SECONDS
            )
        except DeliveryError as error:
            if getattr(error.frame, "reply_code", None) == AMQP_NO_ROUTE:
                raise UnroutableError(f"no queue {queue}") from error
            raise


async def _ack(message: AbstractIncomingMessage) -> bool:
    try:
        await message.ack()
    except ChannelInvalidStateError:
        return False
    return True


def _parse[M: BaseModel](model: type[M], message: AbstractIncomingMessage) -> M | None:
    try:
        return model.model_validate_json(message.body)
    except ValidationError:
        logger.warning("dropping malformed %s message", model.__name__)
        return None
