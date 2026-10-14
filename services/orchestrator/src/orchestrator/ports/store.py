from typing import Protocol
from uuid import UUID

from orchestrator.domain.models import Attempt, Order


class OrderStore(Protocol):
    """Orders as the API reads and creates them. Stage workers write progress and history."""

    async def create_order(self, order: Order) -> None:
        """Insert the order; raise DuplicateIdempotencyKey when its key is taken."""
        ...

    async def get_order(self, order_id: UUID) -> Order | None: ...

    async def get_order_by_key(self, idempotency_key: str) -> Order | None: ...

    async def get_order_with_attempts(self, order_id: UUID) -> tuple[Order, list[Attempt]] | None:
        """The order and its attempts in start order, read from one snapshot."""
        ...
