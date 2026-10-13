import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa
from orchestrator.adapters.postgres_store import PostgresStore, orders
from orchestrator.domain.models import OrderState
from orchestrator.domain.transitions import Succeeded, TimedOut, transition

pytestmark = pytest.mark.contract


async def test_claim_due_skips_an_order_another_transaction_holds(
    fresh_postgres, make_order, dispatch, now
):
    engine = await fresh_postgres()
    store = PostgresStore(engine)
    order = make_order()
    await store.create_order(order)

    async with engine.begin() as holder:
        await holder.execute(sa.select(orders).where(orders.c.id == order.id).with_for_update())
        async with asyncio.timeout(2):
            assert await store.claim_due(now, 10, dispatch) == []

    assert len(await store.claim_due(now, 10, dispatch)) == 1
    await engine.dispose()


async def test_claim_overdue_skips_an_order_another_transaction_holds(
    fresh_postgres, make_order, dispatch, policy, now
):
    engine = await fresh_postgres()
    store = PostgresStore(engine)
    order = make_order()
    await store.create_order(order)
    await store.claim_due(now, 10, dispatch)
    after = now + policy.dispatch_deadline + timedelta(seconds=1)

    def expire(held_order, attempt):
        return transition(held_order, TimedOut(attempt.key, after), policy)

    async with engine.begin() as holder:
        await holder.execute(sa.select(orders).where(orders.c.id == order.id).with_for_update())
        async with asyncio.timeout(2):
            assert await store.claim_overdue(after, 10, expire) == []

    assert len(await store.claim_overdue(after, 10, expire)) == 1
    assert (await store.list_attempts(order.id))[0].finished_at is not None
    await engine.dispose()


async def test_an_attempt_without_stage_output_stores_sql_null(
    fresh_postgres, make_order, dispatch, settle_with, now
):
    engine = await fresh_postgres()
    store = PostgresStore(engine)
    await store.create_order(make_order(stages=("inventory",)))
    [claim] = await store.claim_due(now, 10, dispatch)
    await store.settle(claim.attempt.key, settle_with(lambda a: Succeeded(a.key, now)))

    async with engine.connect() as connection:
        nulls = await connection.scalar(
            sa.text("SELECT count(*) FROM attempts WHERE stage_output IS NULL")
        )
    await engine.dispose()

    assert nulls == 1


async def test_settle_waits_for_a_held_order_lock_instead_of_skipping_it(
    fresh_postgres, make_order, dispatch, settle_with, now
):
    engine = await fresh_postgres()
    store = PostgresStore(engine)
    order = make_order(stages=("inventory",))
    await store.create_order(order)
    [claim] = await store.claim_due(now, 10, dispatch)
    succeed = settle_with(lambda a: Succeeded(a.key, now))

    async with engine.begin() as holder:
        await holder.execute(sa.select(orders).where(orders.c.id == order.id).with_for_update())
        settling = asyncio.create_task(store.settle(claim.attempt.key, succeed))
        await asyncio.sleep(0.5)
        assert not settling.done()

    async with asyncio.timeout(5):
        settled = await settling
    assert settled.state is OrderState.COMPLETED
    await engine.dispose()
