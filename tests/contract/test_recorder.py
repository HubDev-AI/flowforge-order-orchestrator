from datetime import timedelta

import pytest
from contracts import Outcome
from orchestrator import recorder
from orchestrator.adapters.postgres_store import PostgresStore
from orchestrator.domain.models import OrderState

pytestmark = pytest.mark.contract

POLICY = {
    "max_attempts": 3,
    "backoff_base_seconds": 1.0,
    "backoff_cap_seconds": 30.0,
    "deadline_seconds": 10.0,
    "max_wait_seconds": 60.0,
}
PLAN = [{"name": name, "policy": POLICY} for name in ("inventory", "fraud")]
SUBMISSION = {"payload": {"sku": "A-1"}}


@pytest.fixture
def compensations(monkeypatch):
    started = []
    monkeypatch.setattr(recorder, "compensate", lambda *args: started.append(args))
    return started


@pytest.fixture
async def recorded(recorder_database, make_order, now, compensations):
    engine = await recorder_database()
    store = PostgresStore(engine)
    order = make_order(stages=("inventory", "fraud"))
    await store.create_order(order)

    async def read():
        return await store.get_order_with_attempts(order.id)

    yield str(order.id), read, now
    await engine.dispose()


def start(order_id, stage, kind, attempt, now):
    recorder.start(order_id, stage, kind, attempt, now, now + timedelta(seconds=10))


async def test_an_execute_run_records_every_attempt_and_completes_the_order(recorded):
    order_id, read, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    recorder.succeed(order_id, PLAN, 0, "execute", 1, {"reservation_id": "r-1"}, now)
    start(order_id, "fraud", "execute", 1, now)
    recorder.retry(order_id, "fraud", "execute", 1, Outcome.RETRY, "flaky", now, now)
    start(order_id, "fraud", "execute", 2, now)
    recorder.succeed(order_id, PLAN, 1, "execute", 2, None, now)

    order, attempts = await read()

    assert (order.state, order.current_stage, order.next_due_at) == (
        OrderState.COMPLETED,
        None,
        None,
    )
    assert [(a.stage, a.attempt, a.outcome, a.stage_output) for a in attempts] == [
        ("inventory", 1, Outcome.SUCCEEDED, {"reservation_id": "r-1"}),
        ("fraud", 1, Outcome.RETRY, None),
        ("fraud", 2, Outcome.SUCCEEDED, None),
    ]


async def test_a_retry_shows_when_the_next_attempt_is_due(recorded):
    order_id, read, now = recorded
    due = now + timedelta(seconds=2)
    start(order_id, "inventory", "execute", 1, now)
    recorder.retry(
        order_id, "inventory", "execute", 1, Outcome.TIMED_OUT, "deadline passed", now, due
    )

    order, [attempt] = await read()

    assert (order.state, order.next_due_at) == (OrderState.RUNNING, due)
    assert (attempt.outcome, attempt.reason) == (Outcome.TIMED_OUT, "deadline passed")


async def test_a_redelivered_attempt_restarts_its_row_instead_of_failing(recorded):
    order_id, read, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    start(order_id, "inventory", "execute", 1, now + timedelta(seconds=1))

    _, attempts = await read()

    assert [(a.attempt, a.started_at, a.is_open) for a in attempts] == [
        (1, now + timedelta(seconds=1), True)
    ]


async def test_a_failed_execute_starts_compensation_with_each_stage_output(recorded, compensations):
    order_id, _, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    recorder.succeed(order_id, PLAN, 0, "execute", 1, {"reservation_id": "r-1"}, now)
    start(order_id, "fraud", "execute", 1, now)
    recorder.fail(order_id, PLAN, 1, "execute", 1, Outcome.REJECTED, "no", now, SUBMISSION)

    [(compensated_order, plan, positions, submission, outputs)] = compensations
    assert (compensated_order, positions, submission) == (order_id, [1, 0], SUBMISSION)
    assert outputs == {0: {"reservation_id": "r-1"}, 1: None}


async def test_a_finish_recorded_before_its_start_is_not_reopened_by_the_late_start(recorded):
    order_id, read, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    recorder.succeed(order_id, PLAN, 0, "execute", 1, None, now)
    recorder.succeed(order_id, PLAN, 1, "execute", 1, {"score": 1}, now)
    start(order_id, "fraud", "execute", 1, now)

    order, attempts = await read()

    assert order.state is OrderState.COMPLETED
    assert [(a.stage, a.outcome) for a in attempts] == [
        ("inventory", Outcome.SUCCEEDED),
        ("fraud", Outcome.SUCCEEDED),
    ]


async def test_a_rejection_compensates_and_the_last_undo_fails_the_order(recorded):
    order_id, read, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    recorder.succeed(order_id, PLAN, 0, "execute", 1, None, now)
    start(order_id, "fraud", "execute", 1, now)
    recorder.fail(order_id, PLAN, 1, "execute", 1, Outcome.REJECTED, "no", now, SUBMISSION)
    compensating, _ = await read()
    start(order_id, "fraud", "undo", 1, now)
    recorder.succeed(order_id, PLAN, 1, "undo", 1, None, now)
    start(order_id, "inventory", "undo", 1, now)
    recorder.succeed(order_id, PLAN, 0, "undo", 1, None, now)

    order, attempts = await read()

    assert (compensating.state, compensating.current_stage) == (OrderState.COMPENSATING, "fraud")
    assert (order.state, order.terminal_reason) == (OrderState.FAILED, "fraud rejected: no")
    assert [(a.stage, a.kind) for a in attempts][-2:] == [("fraud", "undo"), ("inventory", "undo")]


async def test_an_undo_that_gives_up_ends_in_compensation_failed_with_both_reasons(recorded):
    order_id, read, now = recorded
    start(order_id, "fraud", "execute", 1, now)
    recorder.fail(order_id, PLAN, 1, "execute", 1, Outcome.REJECTED, "no", now, SUBMISSION)
    start(order_id, "fraud", "undo", 3, now)
    recorder.fail(order_id, PLAN, 1, "undo", 3, Outcome.EXHAUSTED, "stuck", now, SUBMISSION)

    order, _ = await read()

    assert order.state is OrderState.COMPENSATION_FAILED
    assert order.terminal_reason == "fraud rejected: no; undo of fraud exhausted: stuck"


async def test_each_handoff_records_when_the_work_started_waiting(recorded):
    order_id, read, now = recorded
    later = now + timedelta(seconds=3)
    start(order_id, "inventory", "execute", 1, now)
    started, _ = await read()
    recorder.succeed(order_id, PLAN, 0, "execute", 1, None, later)
    handed_off, _ = await read()

    assert started.waiting_since is None
    assert handed_off.waiting_since == later


async def test_giving_up_a_stage_that_never_ran_undoes_only_the_stages_before_it(
    recorded, compensations
):
    order_id, read, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    recorder.succeed(order_id, PLAN, 0, "execute", 1, {"reservation_id": "r-1"}, now)

    recorder.give_up(order_id, PLAN, 1, "execute", now, now, SUBMISSION)

    order, _ = await read()
    [(_, _, positions, _, outputs)] = compensations
    assert (order.state, order.current_stage) == (OrderState.COMPENSATING, "inventory")
    assert order.terminal_reason == "fraud unavailable: no worker took it within 60 s"
    assert (positions, outputs) == ([0], {0: {"reservation_id": "r-1"}})


async def test_giving_up_before_any_stage_ran_fails_the_order_with_nothing_to_undo(
    recorded, compensations
):
    order_id, read, now = recorded
    order, _ = await read()

    recorder.give_up(order_id, PLAN, 0, "execute", order.waiting_since, now, SUBMISSION)

    order, _ = await read()
    assert (order.state, order.current_stage) == (OrderState.FAILED, None)
    assert compensations == []


async def test_giving_up_an_undo_ends_in_compensation_failed(recorded):
    order_id, read, now = recorded
    start(order_id, "fraud", "execute", 1, now)
    recorder.fail(order_id, PLAN, 1, "execute", 1, Outcome.REJECTED, "no", now, SUBMISSION)

    recorder.give_up(order_id, PLAN, 1, "undo", now, now, SUBMISSION)

    order, _ = await read()
    assert order.state is OrderState.COMPENSATION_FAILED
    assert order.terminal_reason.endswith(
        "undo of fraud unavailable: no worker took it within 60 s"
    )


async def test_a_give_up_for_a_wait_that_already_ended_changes_nothing(recorded, compensations):
    order_id, read, now = recorded
    start(order_id, "inventory", "execute", 1, now)
    recorder.succeed(order_id, PLAN, 0, "execute", 1, None, now)
    start(order_id, "fraud", "execute", 1, now + timedelta(seconds=1))

    recorder.give_up(order_id, PLAN, 1, "execute", now, now, SUBMISSION)

    order, _ = await read()
    assert order.state is OrderState.RUNNING
    assert compensations == []
