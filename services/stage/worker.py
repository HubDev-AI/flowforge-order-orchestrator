import asyncio
import importlib
import logging
import os
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import recorder
from billiard.exceptions import SoftTimeLimitExceeded
from celery import Celery, chain
from contracts import (
    COMPENSATE_TASK,
    STAGE_TASK,
    Command,
    CommandKind,
    Outcome,
    OutcomeScript,
    ScriptToken,
    stage_signature,
    utc_now,
)
from stage_kit import Simulator, StageHandler, StageSettings

logger = logging.getLogger(__name__)

SIMULATED_TRANSIENT = "simulated transient failure"
SIMULATED_REJECTION = "simulated rejection"
DEADLINE_PASSED = "deadline passed"

app = Celery("flowforge")
app.conf.update(
    broker_url=os.environ.get("RABBITMQ_URL"),
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    worker_enable_remote_control=False,
)


class UnknownStage(Exception):
    pass


class PermanentFailure(Exception):
    """The stage rejected the work or used up its attempts; the order must be compensated."""


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
    """Run one attempt: the simulator's decision, then the handler. Never raises."""
    try:
        match asyncio.run(sim.decide(command)):
            case ScriptToken.RETRY:
                return Outcome.RETRY, SIMULATED_TRANSIENT, None
            case ScriptToken.REJECT:
                return Outcome.REJECTED, SIMULATED_REJECTION, None
        work = handler.undo if command.kind is CommandKind.UNDO else handler.execute
        output = asyncio.run(work(command))
    except SoftTimeLimitExceeded:
        return Outcome.TIMED_OUT, DEADLINE_PASSED, None
    except Exception as error:
        return Outcome.RETRY, type(error).__name__, None
    if utc_now() > command.deadline_at:
        return Outcome.TIMED_OUT, DEADLINE_PASSED, None
    return Outcome.SUCCEEDED, None, output


def backoff_seconds(policy: dict[str, float], attempt: int) -> float:
    return min(policy["backoff_cap_seconds"], policy["backoff_base_seconds"] * 2 ** (attempt - 1))


@app.task(bind=True, name=STAGE_TASK, max_retries=None)
def run_stage(
    self,
    *,
    order_id: str,
    position: int,
    kind: str,
    plan: list[dict[str, Any]],
    submission: dict[str, Any],
) -> None:
    step = plan[position]
    stage, policy = step["name"], step["policy"]
    attempt = self.request.retries + 1
    started = datetime.now(UTC)
    deadline_at = started + timedelta(seconds=policy["deadline_seconds"])
    recorder.start(order_id, stage, kind, attempt, started, deadline_at)
    prior = recorder.execute_output(order_id, stage) if kind == CommandKind.UNDO else None
    command = Command(
        order_id=order_id,
        stage=stage,
        attempt=attempt,
        kind=CommandKind(kind),
        deadline_at=deadline_at,
        payload=submission["payload"],
        script=OutcomeScript.model_validate(submission.get("script") or {}),
        prior_stage_output=prior,
    )
    outcome, reason, output = decide(command, load_handler(stage), simulator())
    logger.info("order %s %s %s attempt %s: %s", order_id, stage, kind, attempt, outcome)
    now = datetime.now(UTC)
    if outcome is Outcome.SUCCEEDED:
        recorder.succeed(order_id, plan, position, kind, attempt, output, now)
        return
    if outcome is Outcome.REJECTED:
        recorder.fail(order_id, stage, kind, attempt, Outcome.REJECTED, reason, now)
        raise PermanentFailure(f"{stage} rejected: {reason}")
    if attempt < policy["max_attempts"]:
        countdown = backoff_seconds(policy, attempt)
        recorder.retry(
            order_id, stage, kind, attempt, outcome, reason, now, now + timedelta(seconds=countdown)
        )
        raise self.retry(countdown=countdown)
    recorder.fail(order_id, stage, kind, attempt, Outcome.EXHAUSTED, reason, now)
    raise PermanentFailure(f"{stage} exhausted: {reason}")


@app.task(name=COMPENSATE_TASK)
def compensate(request, exc, traceback) -> None:
    """Errback of every execute step: undo each dispatched stage in reverse, the failing one first.

    Runs inside the worker whose step failed. An unexpected error is logged and leaves the order
    as it is, since its history was not recorded.
    """
    kwargs = request.kwargs
    if not isinstance(exc, PermanentFailure):
        logger.error("order %s stopped on an unexpected %s", kwargs["order_id"], type(exc).__name__)
        return
    if kwargs["kind"] != CommandKind.EXECUTE:
        return
    undo = [
        stage_signature(
            app,
            order_id=kwargs["order_id"],
            position=position,
            kind=CommandKind.UNDO,
            plan=kwargs["plan"],
            submission=kwargs["submission"],
        )
        for position in range(kwargs["position"], -1, -1)
    ]
    chain(*undo).apply_async()
