from datetime import datetime
from uuid import UUID

from contracts import AttemptKey, CommandKind

from orchestrator.domain.errors import DuplicateIdempotencyKey
from orchestrator.domain.models import Attempt, Decision, Order
from orchestrator.ports.store import Claim, Dispatcher, Resolver


class MemoryStore:
    """Dict-backed OrderStore. Methods never await mid-way, so each runs atomically."""

    def __init__(self) -> None:
        self._orders: dict[UUID, Order] = {}
        self._order_ids_by_key: dict[str, UUID] = {}
        self._attempts: dict[UUID, list[Attempt]] = {}

    async def create_order(self, order: Order) -> None:
        if order.idempotency_key in self._order_ids_by_key:
            raise DuplicateIdempotencyKey(order.idempotency_key)
        self._orders[order.id] = order
        self._order_ids_by_key[order.idempotency_key] = order.id
        self._attempts[order.id] = []

    async def get_order(self, order_id: UUID) -> Order | None:
        return self._orders.get(order_id)

    async def get_order_by_key(self, idempotency_key: str) -> Order | None:
        order_id = self._order_ids_by_key.get(idempotency_key)
        return None if order_id is None else self._orders[order_id]

    async def list_attempts(self, order_id: UUID) -> list[Attempt]:
        return list(self._attempts.get(order_id, []))

    async def claim_due(self, now: datetime, limit: int, dispatch: Dispatcher) -> list[Claim]:
        due = sorted(
            (order for order in self._orders.values() if self._is_due(order, now)),
            key=lambda order: (order.next_due_at, order.created_at),
        )[:limit]
        plans = [(order, dispatch(order)) for order in due]
        claims = []
        for order, plan in plans:
            attempt = Attempt(
                order_id=order.id,
                stage=plan.attempt.stage,
                kind=plan.attempt.kind,
                attempt=self._next_number(order.id, plan.attempt.stage, plan.attempt.kind),
                deadline_at=plan.attempt.deadline_at,
                started_at=now,
            )
            updated = order.apply(plan.decision, now)
            self._orders[order.id] = updated
            self._attempts[order.id].append(attempt)
            claims.append(Claim(updated, attempt))
        return claims

    async def claim_overdue(self, now: datetime, limit: int, resolve: Resolver) -> list[Order]:
        overdue = sorted(
            (
                attempt
                for attempts in self._attempts.values()
                for attempt in attempts
                if attempt.is_open and attempt.deadline_at < now
            ),
            key=lambda attempt: attempt.deadline_at,
        )[:limit]
        resolved = [
            (attempt, resolve(self._orders[attempt.order_id], attempt)) for attempt in overdue
        ]
        return [self._close(attempt, decision) for attempt, decision in resolved]

    async def settle(self, key: AttemptKey, resolve: Resolver) -> Order | None:
        attempt = next(
            (a for a in self._attempts.get(key.order_id, []) if a.is_open and a.matches(key)),
            None,
        )
        if attempt is None:
            return None
        return self._close(attempt, resolve(self._orders[attempt.order_id], attempt))

    def _is_due(self, order: Order, now: datetime) -> bool:
        return (
            order.next_due_at is not None
            and order.next_due_at <= now
            and not any(a.is_open for a in self._attempts[order.id])
        )

    def _next_number(self, order_id: UUID, stage: str, kind: CommandKind) -> int:
        return 1 + sum(a.stage == stage and a.kind == kind for a in self._attempts[order_id])

    def _close(self, attempt: Attempt, decision: Decision) -> Order:
        close = decision.close
        if close is None:
            raise ValueError("a decision that settles an attempt must close it")
        attempts = self._attempts[attempt.order_id]
        attempts[attempts.index(attempt)] = attempt.closed_by(close)
        updated = self._orders[attempt.order_id].apply(decision, close.finished_at)
        self._orders[attempt.order_id] = updated
        return updated
