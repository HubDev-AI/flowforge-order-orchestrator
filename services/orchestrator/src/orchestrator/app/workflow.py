import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from contracts import CommandKind, HandOffStamp, record_message, stage_message
from dramatiq import pipeline
from dramatiq.brokers.rabbitmq import RabbitmqBroker

from orchestrator.app.clock import Clock
from orchestrator.app.config import PipelineConfig
from orchestrator.domain.models import Order
from orchestrator.ports.store import OrderStore

logger = logging.getLogger(__name__)

type Launcher = Callable[[Order], Awaitable[None]]
type Reporter = Callable[[dict[str, Any]], Awaitable[None]]

WATCH_INTERVAL_SECONDS = 1.0
WATCH_BATCH_SIZE = 100
GIVE_UP_MARGIN = timedelta(seconds=5)


def plan_for(order: Order, config: PipelineConfig) -> list[dict[str, Any]]:
    """The order's stage list with each stage's retry policy, fixed at submission."""
    plan = []
    for stage in order.stages:
        policy = config.policy_for(stage)
        plan.append(
            {
                "name": stage,
                "policy": {
                    "max_attempts": policy.max_attempts,
                    "backoff_base_seconds": policy.backoff_base.total_seconds(),
                    "backoff_cap_seconds": policy.backoff_cap.total_seconds(),
                    "deadline_seconds": policy.deadline.total_seconds(),
                    "max_wait_seconds": policy.max_wait.total_seconds(),
                },
            }
        )
    return plan


def order_pipeline(broker: RabbitmqBroker, order: Order, config: PipelineConfig) -> pipeline:
    plan = plan_for(order, config)
    messages = [
        stage_message(
            order_id=str(order.id),
            position=position,
            kind=CommandKind.EXECUTE,
            plan=plan,
            submission=order.payload,
        )
        for position in range(len(plan))
    ]
    return pipeline(messages, broker=broker)


def gave_up_event(order: Order, config: PipelineConfig, now: datetime) -> dict[str, Any] | None:
    """The give-up event for an order that waited longer than its stage's max wait, or None.

    The margin past the max wait gives a worker that started the work just in time the chance
    to report its start before the order is given up.
    """
    stage = order.current_stage or order.stages[0]
    if order.waiting_since is None or now - order.waiting_since <= (
        config.policy_for(stage).max_wait + GIVE_UP_MARGIN
    ):
        return None
    kind = order.current_kind or CommandKind.EXECUTE
    return {
        "type": "gave_up",
        "order_id": str(order.id),
        "plan": plan_for(order, config),
        "position": order.stages.index(stage),
        "kind": kind,
        "waiting_since": order.waiting_since.isoformat(),
        "at": now.isoformat(),
        "submission": order.payload,
    }


async def watch_waiting(
    store: OrderStore, report: Reporter, config: PipelineConfig, clock: Clock
) -> None:
    """Gives up orders whose current work no worker took within the stage's max wait."""
    while True:
        try:
            now = clock()
            for order in await store.waiting_orders(WATCH_BATCH_SIZE):
                event = gave_up_event(order, config, now)
                if event is not None:
                    await report(event)
        except Exception:
            logger.exception("checking waiting orders failed")
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)


def dramatiq_workflow(broker_url: str, config: PipelineConfig) -> tuple[Launcher, Reporter]:
    broker = RabbitmqBroker(url=broker_url, confirm_delivery=True)
    broker.add_middleware(HandOffStamp())

    async def launch(order: Order) -> None:
        await asyncio.to_thread(order_pipeline(broker, order, config).run)

    async def report(event: dict[str, Any]) -> None:
        await asyncio.to_thread(broker.enqueue, record_message(event))

    return launch, report
