import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from celery import Celery, chain
from contracts import COMPENSATE_TASK, CommandKind, stage_signature

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


def order_chain(app: Celery, order: Order, config: PipelineConfig):
    plan = plan_for(order, config)
    steps = [
        stage_signature(
            app,
            order_id=str(order.id),
            position=position,
            kind=CommandKind.EXECUTE,
            plan=plan,
            submission=order.payload,
        )
        for position in range(len(plan))
    ]
    return chain(*steps).on_error(app.signature(COMPENSATE_TASK))


def celery_launcher(broker_url: str, config: PipelineConfig) -> Launcher:
    app = Celery("flowforge", broker=broker_url)
    app.conf.update(task_serializer="json", broker_connection_retry_on_startup=True)

    async def launch(order: Order) -> None:
        await asyncio.to_thread(order_chain(app, order, config).apply_async)

    return launch
