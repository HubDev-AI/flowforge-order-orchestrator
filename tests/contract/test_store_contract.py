import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from contracts import CommandKind, Outcome
from orchestrator.domain.errors import DuplicateIdempotencyKey
from orchestrator.domain.models import Decision, Dispatch, NewAttempt, OrderState
from orchestrator.domain.transitions import (
    Rejected,
    Retry,
    Succeeded,
    TimedOut,
    plan_dispatch,
    transition,
)


async def test_created_order_is_returned_intact(store, make_order):
    order = make_order(payload={"sku": "A-1", "qty": 2}, payload_hash="abc")
    await store.create_order(order)

    assert await store.get_order(order.id) == order
    assert await store.get_order_by_key(order.idempotency_key) == order


async def test_unknown_order_and_key_are_none(store, make_order):
    assert await store.get_order(make_order().id) is None
    assert await store.get_order_by_key("nope") is None


async def test_second_create_with_the_same_key_raises(store, make_order):
    await store.create_order(make_order(idempotency_key="k1"))

    with pytest.raises(DuplicateIdempotencyKey):
        await store.create_order(make_order(idempotency_key="k1"))


async def test_claim_due_returns_only_orders_whose_due_time_has_passed(
    store, make_order, dispatch, now
):
    due = make_order(next_due_at=now - timedelta(seconds=1))
    future = make_order(next_due_at=now + timedelta(seconds=30))
    for order in (due, future):
        await store.create_order(order)

    claims = await store.claim_due(now, 10, dispatch)

    assert [claim.order.id for claim in claims] == [due.id]


async def test_claim_opens_the_attempt_and_clears_the_due_time(
    store, make_order, dispatch, now, policy
):
    order = make_order()
    await store.create_order(order)

    [claim] = await store.claim_due(now, 10, dispatch)

    assert claim.order.state is OrderState.RUNNING
    assert claim.order.next_due_at is None
    assert (claim.attempt.stage, claim.attempt.kind, claim.attempt.attempt) == (
        "inventory",
        CommandKind.EXECUTE,
        1,
    )
    assert claim.attempt.deadline_at == now + policy.dispatch_deadline
    assert await store.get_order(order.id) == claim.order
    assert await store.list_attempts(order.id) == [claim.attempt]


async def test_a_claimed_order_is_not_claimed_twice(store, make_order, dispatch, now):
    await store.create_order(make_order())
    await store.claim_due(now, 10, dispatch)

    assert await store.claim_due(now, 10, dispatch) == []


async def test_order_with_an_open_attempt_is_not_claimable_even_when_due(store, make_order, now):
    order = make_order()
    await store.create_order(order)

    def keep_due(_order):
        return Dispatch(
            Decision(OrderState.RUNNING, "inventory", CommandKind.EXECUTE, now, None),
            NewAttempt("inventory", CommandKind.EXECUTE, now + timedelta(seconds=10)),
        )

    await store.claim_due(now, 10, keep_due)

    assert await store.claim_due(now, 10, keep_due) == []
    assert len(await store.list_attempts(order.id)) == 1


async def test_concurrent_claims_of_one_due_order_yield_a_single_claim(
    store, make_order, dispatch, now
):
    await store.create_order(make_order())

    claims = await asyncio.gather(
        store.claim_due(now, 10, dispatch), store.claim_due(now, 10, dispatch)
    )

    assert sorted(len(batch) for batch in claims) == [0, 1]


async def test_claim_limit_leaves_the_rest_claimable(store, make_order, dispatch, now):
    first = make_order(next_due_at=now - timedelta(seconds=2))
    second = make_order(next_due_at=now - timedelta(seconds=1))
    for order in (second, first):
        await store.create_order(order)

    [one] = await store.claim_due(now, 1, dispatch)
    [other] = await store.claim_due(now, 1, dispatch)

    assert (one.order.id, other.order.id) == (first.id, second.id)


async def test_claim_that_fails_midway_claims_nothing(store, make_order, dispatch, now):
    good = make_order(next_due_at=now - timedelta(seconds=2))
    bad = make_order(next_due_at=now - timedelta(seconds=1))
    for order in (good, bad):
        await store.create_order(order)

    def explode_on_bad(order):
        if order.id == bad.id:
            raise RuntimeError("boom")
        return dispatch(order)

    with pytest.raises(RuntimeError):
        await store.claim_due(now, 10, explode_on_bad)

    assert len(await store.claim_due(now, 10, dispatch)) == 2


async def test_attempt_numbers_restart_per_stage_and_kind(
    store, make_order, make_key, dispatch, settle_with, now
):
    order = make_order()
    await store.create_order(order)
    [first] = await store.claim_due(now, 10, dispatch)
    await store.settle(
        first.attempt.key,
        settle_with(lambda a: Retry(a.key, now, "flaky")),
    )
    [second] = await store.claim_due(now + timedelta(seconds=5), 10, dispatch)
    await store.settle(
        second.attempt.key,
        settle_with(lambda a: Rejected(a.key, now, "no")),
    )
    [undo] = await store.claim_due(now, 10, dispatch)

    assert [first.attempt.attempt, second.attempt.attempt] == [1, 2]
    assert (undo.attempt.kind, undo.attempt.attempt) == (CommandKind.UNDO, 1)
    assert [a.attempt for a in await store.list_attempts(order.id)] == [1, 2, 1]


async def test_settle_closes_an_attempt_once(store, make_order, dispatch, settle_with, now):
    order = make_order()
    await store.create_order(order)
    [claim] = await store.claim_due(now, 10, dispatch)
    succeed = settle_with(lambda a: Succeeded(a.key, now, {"reservation_id": "r-1"}))

    first = await store.settle(claim.attempt.key, succeed)
    second = await store.settle(claim.attempt.key, succeed)

    assert first.current_stage == "fraud"
    assert second is None
    [attempt] = await store.list_attempts(order.id)
    assert (attempt.outcome, attempt.stage_output) == (
        Outcome.SUCCEEDED,
        {"reservation_id": "r-1"},
    )
    assert attempt.finished_at == now


async def test_settle_of_an_unknown_attempt_changes_nothing(
    store, make_order, make_key, dispatch, settle_with, now
):
    order = make_order()
    await store.create_order(order)
    await store.claim_due(now, 10, dispatch)
    stale = make_key(order, "inventory", attempt=7)

    result = await store.settle(stale, settle_with(lambda a: Succeeded(a.key, now)))

    assert result is None
    assert (await store.get_order(order.id)).state is OrderState.RUNNING


async def test_settling_the_last_stage_completes_the_order(
    store, make_order, dispatch, settle_with, now
):
    order = make_order(stages=("inventory",))
    await store.create_order(order)
    [claim] = await store.claim_due(now, 10, dispatch)

    settled = await store.settle(claim.attempt.key, settle_with(lambda a: Succeeded(a.key, now)))

    assert settled.state is OrderState.COMPLETED
    assert (await store.get_order(order.id)).state is OrderState.COMPLETED


async def test_claim_overdue_settles_only_open_attempts_past_their_deadline(
    store, make_order, settle_with, now, policy
):
    late, on_time, answered = (make_order(stages=("inventory", "fraud")) for _ in range(3))
    for order in (late, on_time, answered):
        await store.create_order(order)
    slow = replace(policy, dispatch_deadline=timedelta(hours=1))
    claims = await store.claim_due(
        now,
        10,
        lambda order: plan_dispatch(
            order, now, lambda _: slow if order.id == on_time.id else policy
        ),
    )
    answered_claim = next(c for c in claims if c.order.id == answered.id)
    await store.settle(answered_claim.attempt.key, settle_with(lambda a: Succeeded(a.key, now)))
    after = now + policy.dispatch_deadline + timedelta(seconds=1)

    settled = await store.claim_overdue(
        after,
        10,
        lambda order, attempt: transition(order, TimedOut(attempt.key, after), policy),
    )

    assert [order.id for order in settled] == [late.id]
    [attempt] = await store.list_attempts(late.id)
    assert attempt.outcome is Outcome.TIMED_OUT
    assert settled[0].next_due_at == after + policy.backoff_base
    assert (await store.list_attempts(on_time.id))[0].is_open


async def test_releasing_a_claim_removes_the_attempt_and_makes_the_order_due_again(
    store, make_order, dispatch, now
):
    order = make_order()
    await store.create_order(order)
    [claim] = await store.claim_due(now, 10, dispatch)
    retry_at = now + timedelta(seconds=1)

    await store.release(claim, retry_at)

    assert await store.list_attempts(order.id) == []
    released = await store.get_order(order.id)
    assert (released.state, released.next_due_at) == (OrderState.RUNNING, retry_at)
    assert await store.claim_due(now, 10, dispatch) == []
    [again] = await store.claim_due(retry_at, 10, dispatch)
    assert again.attempt.attempt == 1


async def test_releasing_an_attempt_that_was_settled_meanwhile_keeps_its_row(
    store, make_order, dispatch, settle_with, now
):
    order = make_order()
    await store.create_order(order)
    [claim] = await store.claim_due(now, 10, dispatch)
    await store.settle(claim.attempt.key, settle_with(lambda a: Succeeded(a.key, now)))

    await store.release(claim, now + timedelta(seconds=1))

    [attempt] = await store.list_attempts(order.id)
    assert attempt.outcome is Outcome.SUCCEEDED


async def test_an_order_and_its_attempts_are_read_together(
    store, make_order, dispatch, settle_with, now
):
    order = make_order()
    await store.create_order(order)
    assert await store.get_order_with_attempts(order.id) == (order, [])
    [claim] = await store.claim_due(now, 10, dispatch)
    settled = await store.settle(claim.attempt.key, settle_with(lambda a: Succeeded(a.key, now)))
    [second] = await store.claim_due(now, 10, dispatch)

    found = await store.get_order_with_attempts(order.id)

    assert found[0] == second.order
    assert [(a.stage, a.outcome) for a in found[1]] == [
        ("inventory", Outcome.SUCCEEDED),
        ("fraud", None),
    ]
    assert settled.current_stage == "fraud"
    assert await store.get_order_with_attempts(make_order().id) is None
