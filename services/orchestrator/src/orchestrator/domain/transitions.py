from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from contracts import AttemptKey, CommandKind, Outcome

from orchestrator.domain.errors import IllegalTransition
from orchestrator.domain.models import (
    AttemptClose,
    Decision,
    Dispatch,
    NewAttempt,
    Order,
    OrderState,
    StagePolicy,
)

DEADLINE_PASSED = "deadline passed"


@dataclass(frozen=True, slots=True)
class Submitted:
    at: datetime


@dataclass(frozen=True, slots=True)
class Dispatched:
    at: datetime


@dataclass(frozen=True, slots=True)
class Succeeded:
    attempt: AttemptKey
    at: datetime
    output: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Retry:
    attempt: AttemptKey
    at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class TimedOut:
    attempt: AttemptKey
    at: datetime


@dataclass(frozen=True, slots=True)
class Rejected:
    attempt: AttemptKey
    at: datetime
    reason: str


type Event = Submitted | Dispatched | Succeeded | Retry | TimedOut | Rejected

RUNNING, COMPENSATING = OrderState.RUNNING, OrderState.COMPENSATING
EXECUTE, UNDO = CommandKind.EXECUTE, CommandKind.UNDO


def transition(order: Order | None, event: Event, policy: StagePolicy | None = None) -> Decision:
    state = None if order is None else order.state
    match state, event:
        case None, Submitted(at=at):
            return Decision(OrderState.PENDING, None, None, at, None)
        case OrderState.PENDING, Dispatched():
            return Decision(RUNNING, order.stages[0], EXECUTE, None, None)
        case ((OrderState.RUNNING | OrderState.COMPENSATING), Dispatched()):
            return Decision(
                order.state, order.current_stage, order.current_kind, None, order.terminal_reason
            )
        case OrderState.RUNNING, Succeeded():
            return _stage_succeeded(order, event)
        case OrderState.COMPENSATING, Succeeded():
            return _undo_succeeded(order, event)
        case ((OrderState.RUNNING | OrderState.COMPENSATING), Retry(reason=reason)):
            return _transient_failure(order, event, Outcome.RETRY, reason, policy)
        case ((OrderState.RUNNING | OrderState.COMPENSATING), TimedOut()):
            return _transient_failure(order, event, Outcome.TIMED_OUT, DEADLINE_PASSED, policy)
        case ((OrderState.RUNNING | OrderState.COMPENSATING), Rejected(reason=reason)):
            _require_current(order, event.attempt)
            close = AttemptClose(Outcome.REJECTED, reason, None, event.at)
            return _permanent_failure(order, event.at, close, f"rejected: {reason}")
        case _:
            raise IllegalTransition(f"{type(event).__name__} is not valid in state {state}")


def plan_dispatch(
    order: Order, now: datetime, policy_for: Callable[[str], StagePolicy]
) -> Dispatch:
    decision = transition(order, Dispatched(at=now))
    stage, kind = decision.current_stage, decision.current_kind
    if stage is None or kind is None:
        raise IllegalTransition(f"order {order.id} has no stage to dispatch")
    deadline_at = now + policy_for(stage).dispatch_deadline
    return Dispatch(decision, NewAttempt(stage, kind, deadline_at))


def _require_current(order: Order, attempt: AttemptKey) -> None:
    if (attempt.stage, attempt.kind) != (order.current_stage, order.current_kind):
        raise IllegalTransition(
            f"{attempt.kind} of {attempt.stage} is not the current work of order {order.id}"
        )


def _stage_succeeded(order: Order, event: Succeeded) -> Decision:
    _require_current(order, event.attempt)
    close = AttemptClose(Outcome.SUCCEEDED, None, event.output, event.at)
    index = order.stages.index(event.attempt.stage)
    if index == len(order.stages) - 1:
        return Decision(OrderState.COMPLETED, None, None, None, None, close)
    return Decision(RUNNING, order.stages[index + 1], EXECUTE, event.at, None, close)


def _undo_succeeded(order: Order, event: Succeeded) -> Decision:
    _require_current(order, event.attempt)
    close = AttemptClose(Outcome.SUCCEEDED, None, event.output, event.at)
    index = order.stages.index(event.attempt.stage)
    if index == 0:
        return Decision(OrderState.FAILED, None, None, None, order.terminal_reason, close)
    return Decision(
        COMPENSATING, order.stages[index - 1], UNDO, event.at, order.terminal_reason, close
    )


def _transient_failure(
    order: Order,
    event: Retry | TimedOut,
    outcome: Outcome,
    reason: str,
    policy: StagePolicy | None,
) -> Decision:
    _require_current(order, event.attempt)
    if policy is None:
        raise ValueError("a failed attempt needs the stage's retry policy")
    number = event.attempt.attempt
    if number < policy.max_attempts:
        close = AttemptClose(outcome, reason, None, event.at)
        due_at = event.at + _backoff(policy, number)
        return Decision(
            order.state,
            order.current_stage,
            order.current_kind,
            due_at,
            order.terminal_reason,
            close,
        )
    close = AttemptClose(Outcome.EXHAUSTED, reason, None, event.at)
    return _permanent_failure(order, event.at, close, f"exhausted: {reason}")


def _backoff(policy: StagePolicy, attempt: int) -> timedelta:
    return min(policy.backoff_cap, policy.backoff_base * 2 ** (attempt - 1))


def _permanent_failure(order: Order, at: datetime, close: AttemptClose, summary: str) -> Decision:
    stage = order.current_stage
    if order.state is RUNNING:
        return Decision(COMPENSATING, stage, UNDO, at, f"{stage} {summary}", close)
    reason = f"{order.terminal_reason}; undo of {stage} {summary}"
    return Decision(OrderState.COMPENSATION_FAILED, None, None, None, reason, close)
