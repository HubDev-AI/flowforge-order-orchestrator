from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from contracts import CommandKind, Outcome


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
