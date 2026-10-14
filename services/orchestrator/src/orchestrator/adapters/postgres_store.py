from dataclasses import asdict
from uuid import UUID

import sqlalchemy as sa
from contracts import CommandKind, Outcome
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from orchestrator.domain.errors import DuplicateIdempotencyKey
from orchestrator.domain.models import Attempt, Order, OrderState

IDEMPOTENCY_KEY_CONSTRAINT = "uq_orders_idempotency_key"

metadata = sa.MetaData()

orders = sa.Table(
    "orders",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("stages", JSONB, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("current_stage", sa.Text),
    sa.Column("current_kind", sa.Text),
    sa.Column("next_due_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("terminal_reason", sa.Text),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

attempts = sa.Table(
    "attempts",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("order_id", sa.Uuid, nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("outcome", sa.Text),
    sa.Column("reason", sa.Text),
    sa.Column("stage_output", JSONB(none_as_null=True)),
    sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
)


def _order(row: sa.Row) -> Order:
    return Order(
        id=row.id,
        idempotency_key=row.idempotency_key,
        payload=row.payload,
        payload_hash=row.payload_hash,
        stages=tuple(row.stages),
        state=OrderState(row.state),
        current_stage=row.current_stage,
        current_kind=None if row.current_kind is None else CommandKind(row.current_kind),
        next_due_at=row.next_due_at,
        terminal_reason=row.terminal_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _attempt(row: sa.Row) -> Attempt:
    return Attempt(
        order_id=row.order_id,
        stage=row.stage,
        kind=CommandKind(row.kind),
        attempt=row.attempt,
        deadline_at=row.deadline_at,
        started_at=row.started_at,
        outcome=None if row.outcome is None else Outcome(row.outcome),
        reason=row.reason,
        stage_output=row.stage_output,
        finished_at=row.finished_at,
    )


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_order(self, order: Order) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    sa.insert(orders).values(**(asdict(order) | {"stages": list(order.stages)}))
                )
        except IntegrityError as error:
            violated = getattr(error.orig.__cause__, "constraint_name", None)
            if violated == IDEMPOTENCY_KEY_CONSTRAINT:
                raise DuplicateIdempotencyKey(order.idempotency_key) from error
            raise

    async def get_order(self, order_id: UUID) -> Order | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(sa.select(orders).where(orders.c.id == order_id))
            ).first()
        return None if row is None else _order(row)

    async def get_order_by_key(self, idempotency_key: str) -> Order | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.select(orders).where(orders.c.idempotency_key == idempotency_key)
                )
            ).first()
        return None if row is None else _order(row)

    async def get_order_with_attempts(self, order_id: UUID) -> tuple[Order, list[Attempt]] | None:
        async with self._engine.connect() as connection:
            await connection.execution_options(isolation_level="REPEATABLE READ")
            async with connection.begin():
                row = (
                    await connection.execute(sa.select(orders).where(orders.c.id == order_id))
                ).first()
                if row is None:
                    return None
                history = await connection.execute(
                    sa.select(attempts)
                    .where(attempts.c.order_id == order_id)
                    .order_by(attempts.c.id)
                )
                return _order(row), [_attempt(a) for a in history]
