import asyncio
import logging

from bus import MessageBus, consume_forever
from contracts import AttemptKey, Outcome, Result

from orchestrator.app.clock import Clock
from orchestrator.app.config import PipelineConfig
from orchestrator.domain.transitions import (
    Event,
    Rejected,
    Retry,
    Succeeded,
    TimedOut,
    transition,
)
from orchestrator.ports.store import OrderStore

logger = logging.getLogger(__name__)

NO_REASON = "no reason given"


class ResultsConsumer:
    def __init__(
        self,
        store: OrderStore,
        bus: MessageBus,
        config: PipelineConfig,
        clock: Clock,
        nudge: asyncio.Event,
    ) -> None:
        self._store = store
        self._bus = bus
        self._config = config
        self._clock = clock
        self._nudge = nudge

    async def handle(self, result: Result) -> bool:
        now = self._clock()
        attempt = AttemptKey(
            order_id=result.order_id, stage=result.stage, attempt=result.attempt, kind=result.kind
        )
        event: Event
        match result.outcome:
            case Outcome.SUCCEEDED:
                event = Succeeded(attempt, now, result.stage_output)
            case Outcome.RETRY:
                event = Retry(attempt, now, result.reason or NO_REASON)
            case Outcome.REJECTED:
                event = Rejected(attempt, now, result.reason or NO_REASON)
            case _:
                logger.warning("ignoring a %s result from stage %s", result.outcome, result.stage)
                return False
        policy = self._config.policy_for(result.stage)

        def resolve(order, open_attempt):
            late = open_attempt.deadline_at < now
            return transition(order, TimedOut(attempt, now) if late else event, policy)

        settled = await self._store.settle(attempt, resolve)
        if settled is None:
            return False
        self._nudge.set()
        return True

    async def run(self) -> None:
        await consume_forever(self._bus.consume_results, self.handle)
