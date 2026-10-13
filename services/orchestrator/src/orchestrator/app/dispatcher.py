import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from bus import MessageBus
from contracts import Command, CommandKind, Outcome

from orchestrator.app.clock import Clock
from orchestrator.app.config import PipelineConfig
from orchestrator.app.schemas import OrderRequest
from orchestrator.domain.models import Attempt, Order
from orchestrator.domain.transitions import TimedOut, plan_dispatch, transition
from orchestrator.ports.store import Claim, OrderStore

logger = logging.getLogger(__name__)

CLAIM_BATCH_SIZE = 50


class Dispatcher:
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

    async def tick(self) -> bool:
        """One sweep. Returns True when a batch was full, so more work is probably waiting."""
        now: datetime = self._clock()
        claims = await self._store.claim_due(
            now,
            CLAIM_BATCH_SIZE,
            lambda order: plan_dispatch(order, now, self._config.policy_for),
        )
        await self._publish_all(claims)
        timed_out = await self._store.claim_overdue(
            now,
            CLAIM_BATCH_SIZE,
            lambda order, attempt: transition(
                order, TimedOut(attempt.key, now), self._config.policy_for(attempt.stage)
            ),
        )
        return len(claims) == CLAIM_BATCH_SIZE or len(timed_out) == CLAIM_BATCH_SIZE

    async def run(self) -> None:
        while True:
            busy = False
            try:
                busy = await self.tick()
            except Exception:
                logger.exception("dispatcher tick failed")
            if busy:
                continue
            with suppress(TimeoutError):
                async with asyncio.timeout(self._config.sweep.interval_seconds):
                    await self._nudge.wait()
            self._nudge.clear()

    async def _publish_all(self, claims: list[Claim]) -> None:
        for index, claim in enumerate(claims):
            try:
                await self._bus.publish_command(await self._command(claim.order, claim.attempt))
            except Exception:
                unsent = claims[index:]
                logger.exception(
                    "publishing %s of order %s failed; releasing %s claimed attempts",
                    claim.attempt.stage,
                    claim.order.id,
                    len(unsent),
                )
                retry_at = self._clock() + timedelta(seconds=self._config.sweep.interval_seconds)
                for unpublished in unsent:
                    await self._store.release(unpublished, retry_at)
                return

    async def _command(self, order: Order, attempt: Attempt) -> Command:
        submission = OrderRequest.model_validate(order.payload)
        prior_output: dict[str, Any] | None = None
        if attempt.kind is CommandKind.UNDO:
            prior_output = next(
                (
                    a.stage_output
                    for a in await self._store.list_attempts(order.id)
                    if a.stage == attempt.stage
                    and a.kind is CommandKind.EXECUTE
                    and a.outcome is Outcome.SUCCEEDED
                ),
                None,
            )
        return Command(
            order_id=order.id,
            stage=attempt.stage,
            attempt=attempt.attempt,
            kind=attempt.kind,
            deadline_at=attempt.deadline_at,
            payload=submission.payload,
            script=submission.script,
            prior_stage_output=prior_output,
        )
