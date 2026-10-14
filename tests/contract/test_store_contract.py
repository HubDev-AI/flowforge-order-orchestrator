import pytest
from orchestrator.domain.errors import DuplicateIdempotencyKey


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


async def test_a_new_order_is_read_back_with_an_empty_history(store, make_order):
    order = make_order()
    await store.create_order(order)

    assert await store.get_order_with_attempts(order.id) == (order, [])
    assert await store.get_order_with_attempts(make_order().id) is None
