import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from contracts import CommandKind, Outcome, OutcomeScript
from pydantic import BaseModel, Field

from orchestrator.domain.models import ACTIVE_STATES, Attempt, Order, OrderState
from orchestrator.domain.models import TERMINAL_STATES as TERMINAL

WAITING_NOTICE = timedelta(seconds=2)


class OrderRequest(BaseModel):
    payload: dict[str, Any]
    script: OutcomeScript = Field(default_factory=OutcomeScript)


class SubmissionResponse(BaseModel):
    id: UUID
    state: OrderState


class AttemptView(BaseModel):
    stage: str
    kind: CommandKind
    attempt: int
    outcome: Outcome | None
    reason: str | None
    stage_output: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None


class OrderStatus(BaseModel):
    id: UUID
    state: OrderState
    current_stage: str | None
    next_attempt_due_at: datetime | None
    waiting_for_worker_since: datetime | None
    terminal_reason: str | None
    created_at: datetime
    updated_at: datetime
    attempts: list[AttemptView]


def payload_hash(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def to_status(order: Order, attempts: list[Attempt], now: datetime) -> OrderStatus:
    """`waiting_for_worker_since` is set once handed-off work has waited `WAITING_NOTICE`
    without a worker starting it."""
    waiting_for_worker = (
        order.state in ACTIVE_STATES
        and order.waiting_since is not None
        and now - order.waiting_since >= WAITING_NOTICE
        and not any(a.finished_at is None for a in attempts)
    )
    waiting_to_retry = (
        order.state in (OrderState.RUNNING, OrderState.COMPENSATING)
        and bool(attempts)
        and attempts[-1].outcome in (Outcome.RETRY, Outcome.TIMED_OUT)
    )
    return OrderStatus(
        id=order.id,
        state=order.state,
        current_stage=order.current_stage,
        next_attempt_due_at=order.next_due_at if waiting_to_retry else None,
        waiting_for_worker_since=order.waiting_since if waiting_for_worker else None,
        terminal_reason=order.terminal_reason if order.state in TERMINAL else None,
        created_at=order.created_at,
        updated_at=order.updated_at,
        attempts=[AttemptView.model_validate(a, from_attributes=True) for a in attempts],
    )
