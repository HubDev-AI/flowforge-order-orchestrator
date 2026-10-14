import asyncio
import importlib
import logging
import os
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import dramatiq
from contracts import (
    STAGE_ACTOR,
    Command,
    CommandKind,
    DropAfterMaxWait,
    HandOffStamp,
    Outcome,
    OutcomeScript,
    ScriptToken,
    record_message,
    stage_queue,
    utc_now,
)
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.brokers.stub import StubBroker
from dramatiq.middleware import CurrentMessage
from stage_kit import Simulator, StageHandler, StageSettings

logger = logging.getLogger(__name__)

SIMULATED_TRANSIENT = "simulated transient failure"
SIMULATED_REJECTION = "simulated rejection"
DEADLINE_PASSED = "deadline passed"

_url = os.environ.get("RABBITMQ_URL")
broker = RabbitmqBroker(url=_url, confirm_delivery=True) if _url else StubBroker()
broker.add_middleware(CurrentMessage())
broker.add_middleware(HandOffStamp())
broker.add_middleware(DropAfterMaxWait())
dramatiq.set_broker(broker)


class UnknownStage(Exception):
    pass


class PermanentFailure(Exception):
    """The stage rejected the work or used up its attempts; the order is being compensated."""


def load_handler(name: str) -> StageHandler:
    try:
        module = importlib.import_module(f"handlers.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"handlers.{name}":
            raise
        raise UnknownStage(f"unknown stage {name!r}: there is no handlers/{name}.py") from error
    return module.Handler()


_simulator: Simulator | None = None


def simulator() -> Simulator:
    global _simulator
    if _simulator is None:
        settings = StageSettings()
        _simulator = Simulator(settings.profile, random.Random(settings.seed))
    return _simulator


def decide(
    command: Command, handler: StageHandler, sim: Simulator
) -> tuple[Outcome, str | None, dict[str, Any] | None]:
    """Run one attempt: the simulator's decision, then the handler. Never raises an Exception."""
    try:
        match asyncio.run(sim.decide(command)):
            case ScriptToken.RETRY:
                return Outcome.RETRY, SIMULATED_TRANSIENT, None
            case ScriptToken.REJECT:
                return Outcome.REJECTED, SIMULATED_REJECTION, None
        work = handler.undo if command.kind is CommandKind.UNDO else handler.execute
        output = asyncio.run(work(command))
    except Exception as error:
        return Outcome.RETRY, type(error).__name__, None
    if utc_now() > command.deadline_at:
        return Outcome.TIMED_OUT, DEADLINE_PASSED, None
    return Outcome.SUCCEEDED, None, output


def backoff_seconds(policy: dict[str, float], attempt: int) -> float:
    return min(policy["backoff_cap_seconds"], policy["backoff_base_seconds"] * 2 ** (attempt - 1))


def emit(event: dict[str, Any]) -> None:
    broker.enqueue(record_message(event))


@dramatiq.actor(
    actor_name=STAGE_ACTOR,
    queue_name=stage_queue(os.environ.get("STAGE_NAME", "unset")),
    max_retries=0,
    throws=(PermanentFailure,),
)
def run_stage(
    *,
    order_id: str,
    position: int,
    kind: str,
    plan: list[dict[str, Any]],
    submission: dict[str, Any],
    prior_output: dict[str, Any] | None = None,
) -> None:
    step = plan[position]
    stage, policy = step["name"], step["policy"]
    message = CurrentMessage.get_current_message()
    attempt = (message.options.get("retries", 0) if message else 0) + 1
    started = datetime.now(UTC)
    deadline_at = started + timedelta(seconds=policy["deadline_seconds"])
    identity = {"order_id": order_id, "stage": stage, "kind": kind, "attempt": attempt}
    emit(
        identity
        | {"type": "started", "at": started.isoformat(), "deadline_at": deadline_at.isoformat()}
    )
    command = Command(
        order_id=order_id,
        stage=stage,
        attempt=attempt,
        kind=CommandKind(kind),
        deadline_at=deadline_at,
        payload=submission["payload"],
        script=OutcomeScript.model_validate(submission.get("script") or {}),
        prior_stage_output=prior_output if kind == CommandKind.UNDO else None,
    )
    outcome, reason, output = decide(command, load_handler(stage), simulator())
    logger.info("order %s %s %s attempt %s: %s", order_id, stage, kind, attempt, outcome)
    now = datetime.now(UTC)
    finished = identity | {"at": now.isoformat(), "plan": plan, "position": position}
    if outcome is Outcome.SUCCEEDED:
        emit(finished | {"type": "succeeded", "output": output})
        return
    if outcome is not Outcome.REJECTED and attempt < policy["max_attempts"]:
        delay = backoff_seconds(policy, attempt)
        due_at = now + timedelta(seconds=delay)
        emit(
            finished
            | {
                "type": "retry",
                "outcome": outcome.value,
                "reason": reason,
                "due_at": due_at.isoformat(),
            }
        )
        raise dramatiq.Retry(delay=int(delay * 1000))
    final = Outcome.REJECTED if outcome is Outcome.REJECTED else Outcome.EXHAUSTED
    emit(
        finished
        | {"type": "failed", "outcome": final.value, "reason": reason, "submission": submission}
    )
    raise PermanentFailure(f"{stage} {final.value}: {reason}")
