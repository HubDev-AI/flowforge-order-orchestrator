import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import worker
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
ORDER_ID = str(uuid4())


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
    ],
    ids=["success", "scripted-retry", "scripted-reject", "handler-error"],
)
def test_one_attempt_maps_to_one_outcome(script, handler, expected):
    assert worker.decide(command(script), handler, sim()) == expected


def test_an_answer_after_the_deadline_is_a_timeout():
    late = command(deadline_in=-1)

    assert worker.decide(late, Recording({"score": 1}), sim())[0] is Outcome.TIMED_OUT


def test_the_backoff_doubles_per_attempt_up_to_the_cap():
    assert [worker.backoff_seconds(POLICY, n) for n in (1, 2, 3, 4)] == [1, 2, 4, 4]


@pytest.fixture
def events(monkeypatch):
    sent = []
    monkeypatch.setattr(worker, "emit", sent.append)
    monkeypatch.setattr(worker, "simulator", sim)
    return sent


def run(script=None, kind=CommandKind.EXECUTE, position=1, prior_output=None):
    worker.run_stage.fn(
        order_id=ORDER_ID,
        position=position,
        kind=kind,
        plan=PLAN,
        submission={"payload": {}, "script": script or {}},
        prior_output=prior_output,
    )


def test_a_successful_attempt_reports_its_start_and_its_output(events):
    run()

    assert [e["type"] for e in events] == ["started", "succeeded"]
    assert (events[1]["stage"], events[1]["attempt"]) == ("fraud", 1)
    assert "score" in events[1]["output"]


def test_a_transient_failure_reports_the_retry_and_asks_dramatiq_to_retry(events):
    with pytest.raises(worker.dramatiq.Retry) as retry:
        run({"fraud": {"execute": ["retry"]}})

    assert events[-1]["type"] == "retry"
    assert events[-1]["outcome"] == "retry"
    assert retry.value.delay == 1000


def test_a_rejection_reports_the_failure_and_stops_the_pipeline(events):
    with pytest.raises(worker.PermanentFailure):
        run({"fraud": {"execute": ["reject"]}})

    assert (events[-1]["type"], events[-1]["outcome"]) == ("failed", "rejected")
    assert events[-1]["submission"]["script"] == {"fraud": {"execute": ["reject"]}}


def test_an_undo_hands_the_execute_output_to_the_handler(events, monkeypatch):
    seen = []

    class Undo(StageHandler):
        async def execute(self, command):
            return None

        async def undo(self, command):
            seen.append(command.prior_stage_output)

    monkeypatch.setattr(worker, "load_handler", lambda name: Undo())

    run(kind=CommandKind.UNDO, position=0, prior_output={"reservation_id": "r-1"})

    assert seen == [{"reservation_id": "r-1"}]
