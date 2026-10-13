import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime

from contracts import Command, Result, utc_now

from bus.port import RESULTS_QUEUE, UnroutableError, log_expired, stage_queue


class MemoryBus:
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._queues: dict[str, asyncio.Queue[bytes]] = {RESULTS_QUEUE: asyncio.Queue()}

    async def declare_topology(self, stages: Sequence[str]) -> None:
        for stage in stages:
            self._queues.setdefault(stage_queue(stage), asyncio.Queue())

    async def publish_command(self, command: Command) -> None:
        queue = self._queues.get(stage_queue(command.stage))
        if queue is None:
            raise UnroutableError(f"no queue for stage {command.stage}")
        queue.put_nowait(command.model_dump_json().encode())

    async def publish_result(self, result: Result) -> None:
        self._queues[RESULTS_QUEUE].put_nowait(result.model_dump_json().encode())

    async def consume_results(self) -> AsyncIterator[Result]:
        queue = self._queues[RESULTS_QUEUE]
        while True:
            yield Result.model_validate_json(await queue.get())

    async def consume_commands(self, stage: str) -> AsyncIterator[Command]:
        queue = self._queues.setdefault(stage_queue(stage), asyncio.Queue())
        while True:
            command = Command.model_validate_json(await queue.get())
            if command.deadline_at > self._clock():
                yield command
            else:
                log_expired(command)

    async def close(self) -> None:
        pass
