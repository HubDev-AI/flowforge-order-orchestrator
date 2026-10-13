import logging
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from contracts import Command, Result

logger = logging.getLogger(__name__)

RESULTS_QUEUE = "orchestrator.results"


def stage_queue(stage: str) -> str:
    return f"stage.{stage}"


def log_expired(command: Command) -> None:
    logger.info(
        "dropped expired %s of order %s %s attempt %s",
        command.kind,
        command.order_id,
        command.stage,
        command.attempt,
    )


class UnroutableError(Exception):
    pass


class NotPublished(Exception):
    """The command never left this process: the broker cannot have it.

    Any other publish error is ambiguous, since a broker may accept a message whose
    confirmation is then lost.
    """


class MessageBus(Protocol):
    """Delivery is at least once; the bus never retries a command on its own.

    Orchestrator side: `declare_topology`, `publish_command`, `consume_results`.
    Stage side: `consume_commands`, `publish_result`.
    """

    async def declare_topology(self, stages: Sequence[str]) -> None:
        """Declare the queue of every stage and the results queue; safe to repeat."""
        ...

    async def publish_command(self, command: Command) -> None:
        """Queue a command for its stage.

        Raise NotPublished when nothing was sent, UnroutableError when the stage has no queue.
        """
        ...

    async def publish_result(self, result: Result) -> None: ...

    def consume_results(self) -> AsyncIterator[Result]:
        """Yield results; a result is acknowledged when the consumer asks for the next one."""
        ...

    def consume_commands(self, stage: str) -> AsyncIterator[Command]:
        """Yield live commands of one stage, acknowledged before they are yielded.

        A command whose deadline has already passed is acknowledged and dropped, never yielded.
        """
        ...

    async def close(self) -> None: ...
