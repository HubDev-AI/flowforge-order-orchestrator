from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from contracts import AttemptKey, CommandKind
from orchestrator.domain.models import Order, OrderState, StagePolicy

STAGES = ("inventory", "fraud", "shipping")


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> StagePolicy:
    return StagePolicy(
        max_attempts=3,
        backoff_base=timedelta(seconds=1),
        backoff_cap=timedelta(seconds=30),
        dispatch_deadline=timedelta(seconds=10),
    )


@pytest.fixture
def make_order(now):
    def build(**overrides) -> Order:
        fields = {
            "id": uuid4(),
            "idempotency_key": f"key-{uuid4()}",
            "payload": {"sku": "A-1"},
            "payload_hash": "hash",
            "stages": STAGES,
            "state": OrderState.PENDING,
            "current_stage": None,
            "current_kind": None,
            "next_due_at": now,
            "terminal_reason": None,
            "created_at": now,
            "updated_at": now,
        }
        return Order(**(fields | overrides))

    return build


@pytest.fixture
def make_key():
    def build(order: Order, stage: str, attempt: int = 1, kind=CommandKind.EXECUTE) -> AttemptKey:
        return AttemptKey(order_id=order.id, stage=stage, attempt=attempt, kind=kind)

    return build
