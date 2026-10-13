from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from contracts import AttemptKey, CommandKind, Outcome
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from orchestrator.domain.errors import DuplicateIdempotencyKey
from orchestrator.domain.models import Attempt, Decision, Order, OrderState
from orchestrator.ports.store import Claim, Dispatcher, Resolver

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


def _of_attempt(attempt: Attempt | AttemptKey):
    return sa.and_(
        attempts.c.order_id == attempt.order_id,
        attempts.c.stage == attempt.stage,
        attempts.c.kind == attempt.kind,
        attempts.c.attempt == attempt.attempt,
    )


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_order(self, order: Order) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    sa.insert(orders).values(
                        id=order.id,
                        idempotency_key=order.idempotency_key,
                        payload=order.payload,
                        payload_hash=order.payload_hash,
                        stages=list(order.stages),
                        state=order.state,
                        current_stage=order.current_stage,
                        current_kind=order.current_kind,
                        next_due_at=order.next_due_at,
                        terminal_reason=order.terminal_reason,
                        created_at=order.created_at,
                        updated_at=order.updated_at,
                    )
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

    async def list_attempts(self, order_id: UUID) -> list[Attempt]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                sa.select(attempts).where(attempts.c.order_id == order_id).order_by(attempts.c.id)
            )
            return [_attempt(row) for row in rows]

    async def claim_due(self, now: datetime, limit: int, dispatch: Dispatcher) -> list[Claim]:
        has_open_attempt = sa.exists().where(
            attempts.c.order_id == orders.c.id, attempts.c.finished_at.is_(None)
        )
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                sa.select(orders)
                .where(orders.c.next_due_at <= now, ~has_open_attempt)
                .order_by(orders.c.next_due_at, orders.c.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            plans = [(order, dispatch(order)) for order in map(_order, rows)]
            return [await self._open_attempt(connection, order, plan, now) for order, plan in plans]

    async def claim_overdue(self, now: datetime, limit: int, resolve: Resolver) -> list[Order]:
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                sa.select(attempts)
                .join(orders, orders.c.id == attempts.c.order_id)
                .where(attempts.c.finished_at.is_(None), attempts.c.deadline_at < now)
                .order_by(attempts.c.deadline_at)
                .limit(limit)
                .with_for_update(of=orders, skip_locked=True)
            )
            overdue = [_attempt(row) for row in rows]
            order_rows = await connection.execute(
                sa.select(orders).where(orders.c.id.in_({a.order_id for a in overdue}))
            )
            by_id = {order.id: order for order in map(_order, order_rows)}
            resolved = [(a, resolve(by_id[a.order_id], a)) for a in overdue]
            return [await self._close(connection, a, decision) for a, decision in resolved]

    async def settle(self, key: AttemptKey, resolve: Resolver) -> Order | None:
        async with self._engine.begin() as connection:
            order_row = (
                await connection.execute(
                    sa.select(orders).where(orders.c.id == key.order_id).with_for_update()
                )
            ).first()
            if order_row is None:
                return None
            attempt_row = (
                await connection.execute(
                    sa.select(attempts).where(_of_attempt(key), attempts.c.finished_at.is_(None))
                )
            ).first()
            if attempt_row is None:
                return None
            attempt = _attempt(attempt_row)
            return await self._close(connection, attempt, resolve(_order(order_row), attempt))

    async def _open_attempt(
        self, connection: AsyncConnection, order: Order, plan, now: datetime
    ) -> Claim:
        new = plan.attempt
        taken = await connection.scalar(
            sa.select(sa.func.count()).where(
                attempts.c.order_id == order.id,
                attempts.c.stage == new.stage,
                attempts.c.kind == new.kind,
            )
        )
        row = (
            await connection.execute(
                sa.insert(attempts)
                .values(
                    order_id=order.id,
                    stage=new.stage,
                    kind=new.kind,
                    attempt=taken + 1,
                    deadline_at=new.deadline_at,
                    started_at=now,
                )
                .returning(*attempts.c)
            )
        ).one()
        return Claim(
            await self._write_order(connection, order.id, plan.decision, now), _attempt(row)
        )

    async def _close(
        self, connection: AsyncConnection, attempt: Attempt, decision: Decision
    ) -> Order:
        close = decision.close
        if close is None:
            raise ValueError("a decision that settles an attempt must close it")
        closed = await connection.execute(
            sa.update(attempts)
            .where(_of_attempt(attempt), attempts.c.finished_at.is_(None))
            .values(
                outcome=close.outcome,
                reason=close.reason,
                stage_output=close.stage_output,
                finished_at=close.finished_at,
            )
        )
        if closed.rowcount != 1:
            raise RuntimeError(f"attempt of order {attempt.order_id} was closed concurrently")
        return await self._write_order(connection, attempt.order_id, decision, close.finished_at)

    async def _write_order(
        self, connection: AsyncConnection, order_id: UUID, decision: Decision, at: datetime
    ) -> Order:
        row = (
            await connection.execute(
                sa.update(orders)
                .where(orders.c.id == order_id)
                .values(
                    state=decision.state,
                    current_stage=decision.current_stage,
                    current_kind=decision.current_kind,
                    next_due_at=decision.next_due_at,
                    terminal_reason=decision.terminal_reason,
                    updated_at=at,
                )
                .returning(*orders.c)
            )
        ).one()
        return _order(row)
