from dataclasses import replace
from datetime import timedelta

import pytest
from contracts import CommandKind, Outcome
from orchestrator.domain.errors import IllegalTransition
from orchestrator.domain.models import TERMINAL_STATES, OrderState
from orchestrator.domain.transitions import (
    DEADLINE_PASSED,
    Dispatched,
    Rejected,
    Retry,
    Submitted,
    Succeeded,
    TimedOut,
    plan_dispatch,
    transition,
)

EXECUTE, UNDO = CommandKind.EXECUTE, CommandKind.UNDO


@pytest.fixture
def running_at(make_order, now):
    def build(stage: str, **overrides):
        return make_order(
            state=OrderState.RUNNING,
            current_stage=stage,
            current_kind=EXECUTE,
            next_due_at=None,
            **overrides,
        )

    return build


@pytest.fixture
def compensating_at(make_order, now):
    def build(stage: str, **overrides):
        return make_order(
            state=OrderState.COMPENSATING,
            current_stage=stage,
            current_kind=UNDO,
            next_due_at=None,
            terminal_reason="fraud rejected: score too high",
            **overrides,
        )

    return build


@pytest.fixture
def step(policy):
    def apply(order, event):
        decision = transition(order, event, policy)
        return order.apply(decision, event.at), decision

    return apply


def test_edge_submitted_to_pending(now):
    decision = transition(None, Submitted(at=now))

    assert decision.state is OrderState.PENDING
    assert decision.next_due_at == now
    assert decision.current_stage is None


def test_edge_pending_to_running_on_first_dispatch(make_order, now):
    decision = transition(make_order(), Dispatched(at=now))

    assert decision.state is OrderState.RUNNING
    assert (decision.current_stage, decision.current_kind) == ("inventory", EXECUTE)
    assert decision.next_due_at is None


def test_edge_running_to_running_on_stage_success(running_at, make_key, now, policy):
    order = running_at("inventory")
    key = make_key(order, "inventory")

    decision = transition(order, Succeeded(key, now, {"reservation_id": "r-1"}), policy)

    assert decision.state is OrderState.RUNNING
    assert (decision.current_stage, decision.current_kind) == ("fraud", EXECUTE)
    assert decision.next_due_at == now
    assert decision.close.outcome is Outcome.SUCCEEDED
    assert decision.close.stage_output == {"reservation_id": "r-1"}


def test_edge_running_to_completed_on_last_stage_success(running_at, make_key, now):
    order = running_at("shipping")

    decision = transition(order, Succeeded(make_key(order, "shipping"), now))

    assert decision.state is OrderState.COMPLETED
    assert decision.current_stage is None
    assert decision.next_due_at is None
    assert decision.close.outcome is Outcome.SUCCEEDED


def test_edge_running_to_compensating_on_permanent_failure(running_at, make_key, now):
    order = running_at("fraud")

    decision = transition(order, Rejected(make_key(order, "fraud"), now, "score too high"))

    assert decision.state is OrderState.COMPENSATING
    assert (decision.current_stage, decision.current_kind) == ("fraud", UNDO)
    assert decision.next_due_at == now
    assert decision.close.outcome is Outcome.REJECTED
    assert "score too high" in decision.terminal_reason


def test_edge_compensating_to_failed_when_all_undos_succeeded(compensating_at, make_key, now):
    order = compensating_at("inventory")

    decision = transition(order, Succeeded(make_key(order, "inventory", kind=UNDO), now))

    assert decision.state is OrderState.FAILED
    assert decision.current_stage is None
    assert decision.terminal_reason == order.terminal_reason


def test_edge_compensating_to_compensation_failed_on_rejected_undo(compensating_at, make_key, now):
    order = compensating_at("inventory")

    decision = transition(
        order, Rejected(make_key(order, "inventory", kind=UNDO), now, "cannot release")
    )

    assert decision.state is OrderState.COMPENSATION_FAILED
    assert decision.current_stage is None
    assert decision.next_due_at is None
    assert "cannot release" in decision.terminal_reason
    assert "score too high" in decision.terminal_reason


def test_edge_compensating_to_compensating_when_undos_remain(compensating_at, make_key, now):
    order = compensating_at("fraud")

    decision = transition(order, Succeeded(make_key(order, "fraud", kind=UNDO), now))

    assert decision.state is OrderState.COMPENSATING
    assert (decision.current_stage, decision.current_kind) == ("inventory", UNDO)
    assert decision.next_due_at == now


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_edge_terminal_state_to_end_accepts_no_event(state, make_order, make_key, now):
    order = make_order(state=state, next_due_at=None)
    key = make_key(order, "inventory")
    events = [
        Submitted(now),
        Dispatched(now),
        Succeeded(key, now),
        Retry(key, now, "x"),
        TimedOut(key, now),
        Rejected(key, now, "x"),
    ]

    for event in events:
        with pytest.raises(IllegalTransition):
            transition(order, event)


def test_only_the_diagram_edges_exist(make_order, make_key, now, policy):
    expected = {
        (None, OrderState.PENDING),
        (OrderState.PENDING, OrderState.RUNNING),
        (OrderState.RUNNING, OrderState.RUNNING),
        (OrderState.RUNNING, OrderState.COMPLETED),
        (OrderState.RUNNING, OrderState.COMPENSATING),
        (OrderState.COMPENSATING, OrderState.COMPENSATING),
        (OrderState.COMPENSATING, OrderState.FAILED),
        (OrderState.COMPENSATING, OrderState.COMPENSATION_FAILED),
    }
    sources = [None, make_order()]
    for stage in ("inventory", "fraud", "shipping"):
        sources.append(
            make_order(state=OrderState.RUNNING, current_stage=stage, current_kind=EXECUTE)
        )
        sources.append(
            make_order(state=OrderState.COMPENSATING, current_stage=stage, current_kind=UNDO)
        )
    sources += [make_order(state=state) for state in TERMINAL_STATES]

    observed = set()
    for order in sources:
        stage = order.current_stage if order and order.current_stage else "inventory"
        kind = order.current_kind if order and order.current_kind else EXECUTE
        for number in (1, policy.max_attempts):
            key = make_key(order or make_order(), stage, number, kind)
            for event in (
                Submitted(now),
                Dispatched(now),
                Succeeded(key, now),
                Retry(key, now, "x"),
                TimedOut(key, now),
                Rejected(key, now, "x"),
            ):
                try:
                    decision = transition(order, event, policy)
                except IllegalTransition:
                    continue
                observed.add((order.state if order else None, decision.state))

    assert observed == expected


def test_success_is_illegal_while_pending(make_order, make_key, now):
    order = make_order()

    with pytest.raises(IllegalTransition):
        transition(order, Succeeded(make_key(order, "inventory"), now))


def test_submission_is_illegal_for_an_existing_order(running_at, now):
    with pytest.raises(IllegalTransition):
        transition(running_at("inventory"), Submitted(now))


def test_result_for_another_stage_is_illegal(running_at, make_key, now):
    order = running_at("fraud")

    with pytest.raises(IllegalTransition):
        transition(order, Succeeded(make_key(order, "inventory"), now))


def test_result_of_the_wrong_kind_is_illegal(running_at, make_key, now):
    order = running_at("fraud")

    with pytest.raises(IllegalTransition):
        transition(order, Succeeded(make_key(order, "fraud", kind=UNDO), now))


def test_a_failed_attempt_without_a_retry_policy_is_a_caller_error(running_at, make_key, now):
    order = running_at("fraud")

    with pytest.raises(ValueError, match="retry policy"):
        transition(order, Retry(make_key(order, "fraud"), now, "x"))


def test_two_retries_then_success_advance_to_the_next_stage(running_at, make_key, step, now):
    order = running_at("fraud")
    closes = []

    for event in (
        Retry(make_key(order, "fraud", 1), now, "flaky"),
        Dispatched(now),
        Retry(make_key(order, "fraud", 2), now, "flaky"),
        Dispatched(now),
        Succeeded(make_key(order, "fraud", 3), now, {"score": 3}),
    ):
        order, decision = step(order, event)
        closes.append(decision.close)

    assert [c.outcome for c in closes if c] == [
        Outcome.RETRY,
        Outcome.RETRY,
        Outcome.SUCCEEDED,
    ]
    assert (order.state, order.current_stage) == (OrderState.RUNNING, "shipping")


def test_exhausting_the_first_stage_undoes_that_stage_then_fails(running_at, make_key, step, now):
    order = running_at("inventory")
    outcomes = []

    for number in (1, 2, 3):
        order, decision = step(order, Retry(make_key(order, "inventory", number), now, "down"))
        outcomes.append(decision.close.outcome)
        if number < 3:
            order, _ = step(order, Dispatched(now))

    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.EXHAUSTED]
    assert (order.state, order.current_stage, order.current_kind) == (
        OrderState.COMPENSATING,
        "inventory",
        UNDO,
    )

    order, _ = step(order, Dispatched(now))
    order, _ = step(order, Succeeded(make_key(order, "inventory", 1, UNDO), now))

    assert order.state is OrderState.FAILED
    assert "inventory exhausted: down" in order.terminal_reason


def test_rejection_undoes_the_failing_stage_then_earlier_stages_in_reverse(
    running_at, make_key, step, now
):
    order = running_at("fraud")
    order, _ = step(order, Rejected(make_key(order, "fraud"), now, "score too high"))
    undone = []

    while order.state is OrderState.COMPENSATING:
        undone.append(order.current_stage)
        order, _ = step(order, Dispatched(now))
        order, _ = step(order, Succeeded(make_key(order, order.current_stage, 1, UNDO), now))

    assert undone == ["fraud", "inventory"]
    assert order.state is OrderState.FAILED
    assert order.terminal_reason == "fraud rejected: score too high"


def test_undo_that_keeps_failing_ends_in_compensation_failed(compensating_at, make_key, step, now):
    order = compensating_at("inventory")
    outcomes = []

    for number in (1, 2, 3):
        order, _ = step(order, Dispatched(now))
        order, decision = step(
            order, Retry(make_key(order, "inventory", number, UNDO), now, "stuck")
        )
        outcomes.append(decision.close.outcome)

    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.EXHAUSTED]
    assert order.state is OrderState.COMPENSATION_FAILED
    with pytest.raises(IllegalTransition):
        transition(order, Dispatched(now))


def test_deadline_on_the_final_attempt_is_exhausted_not_timed_out(
    running_at, make_key, now, policy
):
    order = running_at("fraud")

    decision = transition(order, TimedOut(make_key(order, "fraud", 3), now), policy)

    assert decision.close.outcome is Outcome.EXHAUSTED
    assert decision.close.reason == DEADLINE_PASSED
    assert decision.state is OrderState.COMPENSATING


def test_deadline_before_the_final_attempt_is_recorded_and_retried(
    running_at, make_key, now, policy
):
    order = running_at("fraud")

    decision = transition(order, TimedOut(make_key(order, "fraud", 1), now), policy)

    assert decision.close.outcome is Outcome.TIMED_OUT
    assert decision.close.reason == DEADLINE_PASSED
    assert decision.state is OrderState.RUNNING
    assert decision.next_due_at == now + policy.backoff_base


def test_backoff_doubles_per_attempt_up_to_the_cap(running_at, make_key, now, policy):
    order = running_at("fraud")
    policy = replace(policy, max_attempts=10, backoff_cap=timedelta(seconds=4))

    delays = [
        transition(order, Retry(make_key(order, "fraud", n), now, "x"), policy).next_due_at - now
        for n in (1, 2, 3, 4)
    ]

    assert delays == [timedelta(seconds=s) for s in (1, 2, 4, 4)]


def test_stage_list_snapshot_decides_the_next_and_last_stage(running_at, make_key, now):
    order = running_at("inventory", stages=("inventory", "fraud"))

    middle = transition(order, Succeeded(make_key(order, "inventory"), now))
    last_order = running_at("fraud", stages=("inventory", "fraud"))
    last = transition(last_order, Succeeded(make_key(last_order, "fraud"), now))

    assert middle.current_stage == "fraud"
    assert last.state is OrderState.COMPLETED


def test_plan_dispatch_opens_the_first_execute_attempt_with_the_stage_deadline(
    make_order, now, policy
):
    deadlines = {
        "inventory": replace(policy, dispatch_deadline=timedelta(seconds=5)),
        "fraud": policy,
    }

    plan = plan_dispatch(make_order(), now, deadlines.__getitem__)

    assert plan.decision.state is OrderState.RUNNING
    assert (plan.attempt.stage, plan.attempt.kind) == ("inventory", EXECUTE)
    assert plan.attempt.deadline_at == now + timedelta(seconds=5)


def test_plan_dispatch_of_a_compensating_order_opens_an_undo_attempt(compensating_at, now, policy):
    plan = plan_dispatch(compensating_at("fraud"), now, lambda stage: policy)

    assert plan.decision.state is OrderState.COMPENSATING
    assert (plan.attempt.stage, plan.attempt.kind) == ("fraud", UNDO)
