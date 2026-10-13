import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from contracts import CommandKind, Outcome, OutcomeScript
from pydantic import BaseModel, Field

from orchestrator.domain.models import TERMINAL_STATES as TERMINAL
from orchestrator.domain.models import Attempt, Order, OrderState


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
    terminal_reason: str | None
    created_at: datetime
    updated_at: datetime
    attempts: list[AttemptView]


def payload_hash(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def to_status(order: Order, attempts: list[Attempt]) -> OrderStatus:
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
        terminal_reason=order.terminal_reason if order.state in TERMINAL else None,
        created_at=order.created_at,
        updated_at=order.updated_at,
        attempts=[
            AttemptView(
                stage=a.stage,
                kind=a.kind,
                attempt=a.attempt,
                outcome=a.outcome,
                reason=a.reason,
                stage_output=a.stage_output,
                started_at=a.started_at,
                finished_at=a.finished_at,
            )
            for a in attempts
        ],
    )
