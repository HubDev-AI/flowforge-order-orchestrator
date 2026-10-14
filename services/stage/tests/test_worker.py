import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import worker
from billiard.exceptions import SoftTimeLimitExceeded
from contracts import Command, CommandKind, Outcome, OutcomeScript
from stage_kit import Profile, Simulator, StageHandler

QUIET = Profile(latency_min_s=0, latency_max_s=0, transient_probability=0, reject_probability=0)
POLICY = {
    "max_attempts": 3,
    "backoff_base_seconds": 1.0,
    "backoff_cap_seconds": 4.0,
    "deadline_seconds": 10.0,
}
PLAN = [{"name": name, "policy": POLICY} for name in ("inventory", "fraud", "shipping")]


class Recording(StageHandler):
    def __init__(self, output=None, error=None):
        self.output, self.error = output, error

    async def execute(self, command):
        if self.error:
            raise self.error
        return self.output


def command(script=None, kind=CommandKind.EXECUTE, deadline_in=60):
    return Command(
        order_id=uuid4(),
        stage="fraud",
        attempt=1,
        kind=kind,
        deadline_at=datetime.now(UTC) + timedelta(seconds=deadline_in),
        payload={},
        script=OutcomeScript.model_validate(script or {}),
    )


def sim():
    async def no_wait(_seconds):
        return None

    return Simulator(QUIET, random.Random(1), sleep=no_wait)


RETRY_SCRIPT = {"fraud": {"execute": ["retry"]}}
REJECT_SCRIPT = {"fraud": {"execute": ["reject"]}}


@pytest.mark.parametrize(
    ("script", "handler", "expected"),
    [
        (None, Recording({"score": 1}), (Outcome.SUCCEEDED, None, {"score": 1})),
        (RETRY_SCRIPT, Recording(), (Outcome.RETRY, worker.SIMULATED_TRANSIENT, None)),
        (REJECT_SCRIPT, Recording(), (Outcome.REJECTED, worker.SIMULATED_REJECTION, None)),
        (None, Recording(error=ValueError("secret")), (Outcome.RETRY, "ValueError", None)),
        (
            None,
            Recording(error=SoftTimeLimitExceeded()),
            (Outcome.TIMED_OUT, worker.DEADLINE_PASSED, None),
        ),
    ],
    ids=["success", "scripted-retry", "scripted-reject", "handler-error", "soft-time-limit"],
)
def test_one_attempt_maps_to_one_outcome(script, handler, expected):
    assert worker.decide(command(script), handler, sim()) == expected


def test_an_answer_after_the_deadline_is_a_timeout():
    late = command(deadline_in=-1)

    assert worker.decide(late, Recording({"score": 1}), sim())[0] is Outcome.TIMED_OUT


def test_the_backoff_doubles_per_attempt_up_to_the_cap():
    assert [worker.backoff_seconds(POLICY, n) for n in (1, 2, 3, 4)] == [1, 2, 4, 4]


def failed(kind, position):
    request = SimpleNamespace(
        kwargs={
            "order_id": "o-1",
            "position": position,
            "kind": kind,
            "plan": PLAN,
            "submission": {"payload": {}},
        }
    )
    return request


@pytest.fixture
def launched(monkeypatch):
    chains = []

    class Captured:
        def __init__(self, *steps):
            self.steps = steps

        def apply_async(self):
            chains.append(self.steps)

    monkeypatch.setattr(worker, "chain", Captured)
    return chains


def test_a_permanent_execute_failure_undoes_every_dispatched_stage_in_reverse(launched):
    worker.compensate(failed(CommandKind.EXECUTE, 1), worker.PermanentFailure("no"), None)

    [steps] = launched
    assert [(s.kwargs["position"], s.kwargs["kind"]) for s in steps] == [(1, "undo"), (0, "undo")]
    assert [s.options["queue"] for s in steps] == ["stage.fraud", "stage.inventory"]


@pytest.mark.parametrize(
    ("kind", "error"),
    [(CommandKind.UNDO, worker.PermanentFailure("no")), (CommandKind.EXECUTE, RuntimeError("db"))],
    ids=["undo-failure", "unexpected-error"],
)
def test_only_a_permanent_execute_failure_starts_compensation(launched, kind, error):
    worker.compensate(failed(kind, 1), error, None)

    assert launched == []
