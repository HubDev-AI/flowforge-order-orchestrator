from typing import Any

from dramatiq import Message

STAGE_ACTOR = "flowforge.stage"
HARD_LIMIT_GRACE_SECONDS = 5.0


def stage_queue(stage: str) -> str:
    return f"stage.{stage}"


def stage_message(
    *,
    order_id: str,
    position: int,
    kind: str,
    plan: list[dict[str, Any]],
    submission: dict[str, Any],
) -> Message:
    """One attempt series of one stage, on that stage's queue. `plan` is the order's snapshot.

    `max_retries` is the stage's attempt cap, so Dramatiq stops retrying where the policy does.
    The time limit only ends a stage that ignores its deadline; the deadline itself is checked
    by the worker against the time the attempt started.
    """
    step = plan[position]
    policy = step["policy"]
    return Message(
        queue_name=stage_queue(step["name"]),
        actor_name=STAGE_ACTOR,
        args=(),
        kwargs={
            "order_id": order_id,
            "position": position,
            "kind": kind,
            "plan": plan,
            "submission": submission,
        },
        options={
            "max_retries": policy["max_attempts"] - 1,
            "time_limit": int((policy["deadline_seconds"] + HARD_LIMIT_GRACE_SECONDS) * 1000),
            "pipe_ignore": True,
        },
    )
