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

    waiting = to_status(order, [attempt(order, now, Outcome.RETRY)])
    between_stages = to_status(order, [attempt(order, now, Outcome.SUCCEEDED)])
    in_progress = to_status(order, [attempt(order, now, None, finished=False)])

    assert waiting.next_attempt_due_at == due
    assert between_stages.next_attempt_due_at is None
    assert in_progress.next_attempt_due_at is None


def test_the_terminal_reason_shows_only_once_the_order_has_finished(make_order, now):
    reason = "fraud rejected: simulated rejection"
    compensating = make_order(state=OrderState.COMPENSATING, terminal_reason=reason)
    failed = make_order(state=OrderState.FAILED, terminal_reason=reason)

    assert to_status(compensating, []).terminal_reason is None
    assert to_status(failed, []).terminal_reason == reason
