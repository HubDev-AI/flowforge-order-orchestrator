from uuid import UUID

from orchestrator.domain.errors import DuplicateIdempotencyKey
from orchestrator.domain.models import Attempt, Order


class MemoryStore:
    """Dict-backed OrderStore for API tests; nothing writes attempts to it."""

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

    async def get_order_with_attempts(self, order_id: UUID) -> tuple[Order, list[Attempt]] | None:
        order = self._orders.get(order_id)
        return None if order is None else (order, list(self._attempts[order_id]))
