import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from contracts import Command, CommandKind
from handlers import fraud, gift_wrap, inventory, shipping
from main import UnknownStage, load_handler


def command(stage, kind=CommandKind.EXECUTE, prior=None, order_id=None):
    return Command(
        order_id=order_id or uuid4(),
        stage=stage,
        attempt=1,
        kind=kind,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
        payload={},
        prior_stage_output=prior,
    )


async def test_inventory_reserves_stock_under_an_id_derived_from_the_order():
    order_id = uuid4()

    first = await inventory.Handler().execute(command("inventory", order_id=order_id))
    again = await inventory.Handler().execute(command("inventory", order_id=order_id))

    assert first == again
    assert str(order_id) in first["reservation_id"]


async def test_inventory_undo_logs_the_reservation_it_releases(caplog):
    prior = {"reservation_id": "res-42"}

    with caplog.at_level(logging.INFO):
        output = await inventory.Handler().undo(command("inventory", CommandKind.UNDO, prior))

    assert "res-42" in caplog.text
    assert output is None


async def test_fraud_scores_an_order_the_same_way_every_time():
    order_id = uuid4()

    first = await fraud.Handler().execute(command("fraud", order_id=order_id))
    again = await fraud.Handler().execute(command("fraud", order_id=order_id))

    assert first == again
    assert 0 <= first["score"] <= 1


async def test_shipping_creates_a_label_id_derived_from_the_order():
    order_id = uuid4()

    output = await shipping.Handler().execute(command("shipping", order_id=order_id))

    assert str(order_id) in output["label_id"]


async def test_gift_wrap_wraps_the_order():
    order_id = uuid4()

    output = await gift_wrap.Handler().execute(command("gift_wrap", order_id=order_id))

    assert output == {"wrapped": True, "wrap_id": f"wrap-{order_id}"}


@pytest.mark.parametrize("stage", [fraud, shipping])
async def test_a_stage_without_an_undo_override_undoes_nothing(stage):
    assert await stage.Handler().undo(command("x", CommandKind.UNDO)) is None


@pytest.mark.parametrize(
    ("name", "module"),
    [("inventory", inventory), ("fraud", fraud), ("shipping", shipping), ("gift_wrap", gift_wrap)],
)
def test_the_stage_name_selects_its_handler_module(name, module):
    assert isinstance(load_handler(name), module.Handler)


def test_an_unknown_stage_name_fails_startup_with_a_clear_error():
    with pytest.raises(UnknownStage, match="engraving"):
        load_handler("engraving")
