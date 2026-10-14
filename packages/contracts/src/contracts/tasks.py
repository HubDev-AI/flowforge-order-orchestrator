from typing import Any

from dramatiq import Message

STAGE_ACTOR = "flowforge.stage"
RECORD_ACTOR = "flowforge.record"
RECORDS_QUEUE = "orchestrator.records"
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
    prior_output: dict[str, Any] | None = None,
) -> Message:
    """One attempt series of one stage, on that stage's queue. `plan` is the order's snapshot;
    an undo carries the output of the execute it reverses.

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
            "prior_output": prior_output,
        },
        options={
            "max_retries": policy["max_attempts"] - 1,
            "time_limit": int((policy["deadline_seconds"] + HARD_LIMIT_GRACE_SECONDS) * 1000),
            "pipe_ignore": True,
        },
    )


def record_message(event: dict[str, Any]) -> Message:
    """An attempt event for the recorder, the only writer of order state and history."""
    return Message(
        queue_name=RECORDS_QUEUE, actor_name=RECORD_ACTOR, args=(event,), kwargs={}, options={}
    )
