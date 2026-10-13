from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts import AttemptKey

from orchestrator.domain.models import Attempt, Decision, Dispatch, Order

Dispatcher = Callable[[Order], Dispatch]
Resolver = Callable[[Order, Attempt], Decision]


@dataclass(frozen=True, slots=True)
class Claim:
    order: Order
    attempt: Attempt


class OrderStore(Protocol):
    """The only owner of order state.

    `claim_due`, `claim_overdue`, and `settle` run their callback inside one transaction and
    apply every decision it returns, or none: a crash never leaves an attempt without its
    order update. The callback must be pure.
    """

    async def create_order(self, order: Order) -> None:
        """Insert the order; raise DuplicateIdempotencyKey when its key is taken."""
        ...

    async def get_order(self, order_id: UUID) -> Order | None: ...

    async def get_order_by_key(self, idempotency_key: str) -> Order | None: ...

    async def get_order_with_attempts(self, order_id: UUID) -> tuple[Order, list[Attempt]] | None:
        """The order and its attempts read from one snapshot, so the two never disagree."""
        ...

    async def list_attempts(self, order_id: UUID) -> list[Attempt]:
        """Attempts in start order; numbering restarts per stage and kind."""
        ...

    async def claim_due(self, now: datetime, limit: int, dispatch: Dispatcher) -> list[Claim]:
        """Claim up to `limit` orders whose due time has passed and that have no open attempt.

        For each, open the planned attempt (numbered after the stage and kind's last attempt),
        apply the planned decision, and return the updated order with the new attempt. Rows
        another transaction holds are skipped, so concurrent callers never claim the same order.
        """
        ...

    async def release(self, claim: Claim, due_at: datetime) -> None:
        """Take back a claimed attempt whose command was never published.

        Removes the open attempt row and makes the order due again at `due_at`, so the next
        claim reuses the attempt number and nothing counts against the attempt cap.
        """
        ...

    async def claim_overdue(self, now: datetime, limit: int, resolve: Resolver) -> list[Order]:
        """Settle up to `limit` open attempts whose deadline is before `now`.

        `resolve` must return a decision that closes the attempt.
        """
        ...

    async def settle(self, key: AttemptKey, resolve: Resolver) -> Order | None:
        """Close the open attempt `key` names and apply the decision `resolve` returns for it.

        Returns the updated order, or None, changing nothing, when no open attempt matches:
        the result is stale. Closing is guarded, so two settlements of one attempt never both
        succeed.
        """
        ...
