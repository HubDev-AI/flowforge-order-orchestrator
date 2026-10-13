from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from contracts import AttemptKey, CommandKind, Outcome


class OrderState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPENSATING = "compensating"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATION_FAILED = "compensation_failed"


TERMINAL_STATES = frozenset(
    {OrderState.COMPLETED, OrderState.FAILED, OrderState.COMPENSATION_FAILED}
)


@dataclass(frozen=True, slots=True)
class StagePolicy:
    max_attempts: int
    backoff_base: timedelta
    backoff_cap: timedelta
    dispatch_deadline: timedelta


@dataclass(frozen=True, slots=True)
class AttemptClose:
    outcome: Outcome
    reason: str | None
    stage_output: dict[str, Any] | None
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    state: OrderState
    current_stage: str | None
    current_kind: CommandKind | None
    next_due_at: datetime | None
    terminal_reason: str | None
    close: AttemptClose | None = None


@dataclass(frozen=True, slots=True)
class NewAttempt:
    stage: str
    kind: CommandKind
    deadline_at: datetime


@dataclass(frozen=True, slots=True)
class Dispatch:
    decision: Decision
    attempt: NewAttempt


@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    idempotency_key: str
    payload: dict[str, Any]
    payload_hash: str
    stages: tuple[str, ...]
    state: OrderState
    current_stage: str | None
    current_kind: CommandKind | None
    next_due_at: datetime | None
    terminal_reason: str | None
    created_at: datetime
    updated_at: datetime

    def apply(self, decision: Decision, at: datetime) -> Order:
        return replace(
            self,
            state=decision.state,
            current_stage=decision.current_stage,
            current_kind=decision.current_kind,
            next_due_at=decision.next_due_at,
            terminal_reason=decision.terminal_reason,
            updated_at=at,
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    order_id: UUID
    stage: str
    kind: CommandKind
    attempt: int
    deadline_at: datetime
    started_at: datetime
    outcome: Outcome | None = None
    reason: str | None = None
    stage_output: dict[str, Any] | None = None
    finished_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.finished_at is None

    @property
    def key(self) -> AttemptKey:
        return AttemptKey(
            order_id=self.order_id, stage=self.stage, attempt=self.attempt, kind=self.kind
        )

    def matches(self, key: AttemptKey) -> bool:
        return (self.order_id, self.stage, self.kind, self.attempt) == (
            key.order_id,
            key.stage,
            key.kind,
            key.attempt,
        )

    def closed_by(self, close: AttemptClose) -> Attempt:
        return replace(
            self,
            outcome=close.outcome,
            reason=close.reason,
            stage_output=close.stage_output,
            finished_at=close.finished_at,
        )
