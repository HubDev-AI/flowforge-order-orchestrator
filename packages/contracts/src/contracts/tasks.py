import logging
import time
from typing import Any

from dramatiq import Message, Middleware
from dramatiq.middleware import SkipMessage

logger = logging.getLogger(__name__)

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


def _now_millis() -> int:
    return int(time.time() * 1000)


class HandOffStamp(Middleware):
    """Stamps stage work with the time it becomes available to a worker.

    Runs on every enqueue, including a pipeline's next stage and a retry; a delayed retry is
    stamped again when its delay ends and it moves to the stage's queue.
    """

    def before_enqueue(self, broker, message, delay):
        if message.actor_name == STAGE_ACTOR:
            message.options["handed_off_at"] = _now_millis() + (delay or 0)


class DropAfterMaxWait(Middleware):
    """Skips stage work that waited longer than its stage's `max_wait_seconds` for a worker.

    The orchestrator gives the order up after that wait, so running the work would act for an
    order that has moved on.
    """

    def before_process_message(self, broker, message):
        if message.actor_name != STAGE_ACTOR or "handed_off_at" not in message.options:
            return
        kwargs = message.kwargs
        step = kwargs["plan"][kwargs["position"]]
        waited = (_now_millis() - message.options["handed_off_at"]) / 1000
        if waited > step["policy"]["max_wait_seconds"]:
            logger.info(
                "dropped %s of order %s %s after waiting %.0f s for a worker",
                kwargs["kind"],
                kwargs["order_id"],
                step["name"],
                waited,
            )
            message.fail()
            raise SkipMessage("waited longer than the stage's max wait")
