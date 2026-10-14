from typing import Any

from celery import Celery, Signature

STAGE_TASK = "flowforge.stage"
COMPENSATE_TASK = "flowforge.compensate"
HARD_LIMIT_GRACE_SECONDS = 5.0


def stage_queue(stage: str) -> str:
    return f"stage.{stage}"


def stage_signature(
    app: Celery,
    *,
    order_id: str,
    position: int,
    kind: str,
    plan: list[dict[str, Any]],
    submission: dict[str, Any],
) -> Signature:
    """One attempt series of one stage. `plan` is the order's snapshot: name and policy per stage.

    The soft time limit is the stage's dispatch deadline; the hard limit ends a stage that
    ignores it.
    """
    step = plan[position]
    deadline = step["policy"]["deadline_seconds"]
    return app.signature(
        STAGE_TASK,
        kwargs={
            "order_id": order_id,
            "position": position,
            "kind": kind,
            "plan": plan,
            "submission": submission,
        },
        queue=stage_queue(step["name"]),
        immutable=True,
        soft_time_limit=deadline,
        time_limit=deadline + HARD_LIMIT_GRACE_SECONDS,
    )
