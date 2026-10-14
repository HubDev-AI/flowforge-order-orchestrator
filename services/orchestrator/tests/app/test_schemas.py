from datetime import timedelta

from contracts import CommandKind, Outcome
from orchestrator.app.schemas import to_status
from orchestrator.domain.models import Attempt, OrderState


def attempt(order, now, outcome, kind=CommandKind.EXECUTE, finished=True):
    return Attempt(
        order_id=order.id,
        stage="fraud",
        kind=kind,
        attempt=1,
        deadline_at=now + timedelta(seconds=10),
        started_at=now,
        outcome=outcome,
        finished_at=now if finished else None,
    )


def test_the_due_time_shows_only_while_the_order_waits_to_retry(make_order, now):
    due = now + timedelta(seconds=2)
    order = make_order(state=OrderState.RUNNING, current_stage="fraud", next_due_at=due)

    waiting = to_status(order, [attempt(order, now, Outcome.RETRY)], now)
    between_stages = to_status(order, [attempt(order, now, Outcome.SUCCEEDED)], now)
    in_progress = to_status(order, [attempt(order, now, None, finished=False)], now)

    assert waiting.next_attempt_due_at == due
    assert between_stages.next_attempt_due_at is None
    assert in_progress.next_attempt_due_at is None


def test_the_terminal_reason_shows_only_once_the_order_has_finished(make_order, now):
    reason = "fraud rejected: simulated rejection"
    compensating = make_order(state=OrderState.COMPENSATING, terminal_reason=reason)
    failed = make_order(state=OrderState.FAILED, terminal_reason=reason)

    assert to_status(compensating, [], now).terminal_reason is None
    assert to_status(failed, [], now).terminal_reason == reason


def test_the_wait_for_a_worker_shows_once_handed_off_work_sat_unstarted(make_order, now):
    handed_off = now - timedelta(seconds=30)
    order = make_order(state=OrderState.RUNNING, current_stage="fraud", waiting_since=handed_off)

    waiting = to_status(order, [], now)
    just_handed_off = to_status(order, [], handed_off + timedelta(seconds=1))
    started = to_status(order, [attempt(order, now, None, finished=False)], now)
    finished = to_status(make_order(state=OrderState.COMPLETED, waiting_since=handed_off), [], now)

    assert waiting.waiting_for_worker_since == handed_off
    assert just_handed_off.waiting_for_worker_since is None
    assert started.waiting_for_worker_since is None
    assert finished.waiting_for_worker_since is None
