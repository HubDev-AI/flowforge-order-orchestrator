import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from contracts import CommandKind, stage_message
from dramatiq import pipeline
from dramatiq.brokers.rabbitmq import RabbitmqBroker

from orchestrator.app.config import PipelineConfig
from orchestrator.domain.models import Order

type Launcher = Callable[[Order], Awaitable[None]]


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
                    "deadline_seconds": policy.dispatch_deadline.total_seconds(),
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


def dramatiq_launcher(broker_url: str, config: PipelineConfig) -> Launcher:
    broker = RabbitmqBroker(url=broker_url, confirm_delivery=True)

    async def launch(order: Order) -> None:
        await asyncio.to_thread(order_pipeline(broker, order, config).run)

    return launch
