import pytest

pytestmark = pytest.mark.e2e

EXECUTE, UNDO = "execute", "undo"


def in_progress(stage: str, kind: str = EXECUTE):
    def condition(status: dict) -> bool:
        return any(
            a["stage"] == stage and a["kind"] == kind and a["finished_at"] is None
            for a in status["attempts"]
        )

    return condition


def test_an_order_survives_an_orchestrator_restart_while_an_execute_is_outstanding(stack, orders):
    order_id = orders.start({"fraud": {"execute": ["timeout"]}})
    orders.wait_until(order_id, in_progress("fraud"))

    stack.restart("orchestrator")

    done = orders.finish(order_id)
    assert done["state"] == "completed"
    assert orders.history(done) == [
        ("inventory", EXECUTE, 1, "succeeded"),
        ("fraud", EXECUTE, 1, "timed_out"),
        ("fraud", EXECUTE, 2, "succeeded"),
        ("shipping", EXECUTE, 1, "succeeded"),
    ]


def test_a_compensating_order_survives_an_orchestrator_restart_while_an_undo_is_outstanding(
    stack, orders
):
    order_id = orders.start({"fraud": {"execute": ["reject"]}, "inventory": {"undo": ["timeout"]}})
    orders.wait_until(order_id, in_progress("inventory", UNDO))

    stack.restart("orchestrator")

    resumed = orders.status(order_id)
    assert (resumed["state"], resumed["current_stage"]) == ("compensating", "inventory")
    done = orders.finish(order_id)
    assert done["state"] == "failed"
    assert orders.history(done)[-2:] == [
        ("inventory", UNDO, 1, "timed_out"),
        ("inventory", UNDO, 2, "succeeded"),
    ]


def test_a_retry_waiting_out_its_backoff_is_dispatched_after_an_orchestrator_restart(stack, orders):
    order_id = orders.start({"shipping": {"execute": ["retry"]}})
    waiting = orders.wait_until(order_id, lambda s: s["next_attempt_due_at"] is not None)
    assert waiting["current_stage"] == "shipping"

    stack.restart("orchestrator")

    done = orders.finish(order_id)
    assert done["state"] == "completed"
    assert orders.history(done)[-2:] == [
        ("shipping", EXECUTE, 1, "retry"),
        ("shipping", EXECUTE, 2, "succeeded"),
    ]


def test_an_order_in_flight_keeps_its_stage_list_when_the_pipeline_config_changes(
    stack, orders, default_config
):
    old_order = orders.start({"fraud": {"execute": ["timeout"]}})
    orders.wait_until(old_order, in_progress("fraud"))

    stack.configure(stages=["inventory", "shipping"], overrides={})
    stack.restart("orchestrator")
    new_order = orders.start()

    old_done = orders.finish(old_order)
    new_done = orders.finish(new_order)
    assert old_done["state"] == "completed"
    assert {a["stage"] for a in old_done["attempts"]} == {"inventory", "fraud", "shipping"}
    assert new_done["state"] == "completed"
    assert [a["stage"] for a in new_done["attempts"]] == ["inventory", "shipping"]


def test_an_order_completes_when_the_broker_restarts_mid_run(stack, orders):
    order_id = orders.start({"fraud": {"execute": ["timeout"]}})
    orders.wait_until(order_id, in_progress("fraud"))

    stack.restart("rabbitmq")

    done = orders.finish(order_id, timeout=150)
    assert done["state"] == "completed"
    assert {a["stage"] for a in done["attempts"]} == {"inventory", "fraud", "shipping"}


def test_a_new_stage_needs_only_a_handler_a_compose_service_and_a_config_line(
    stack, orders, default_config
):
    stack.compose("up", "-d", "--build", "--wait", "stage-gift-wrap", gift_wrap=True)
    try:
        stack.configure(stages=["inventory", "fraud", "gift_wrap", "shipping"])
        stack.restart("orchestrator")

        done = orders.finish(orders.start())
    finally:
        stack.compose("rm", "-sf", "stage-gift-wrap", gift_wrap=True, check=False)

    assert done["state"] == "completed"
    assert [a["stage"] for a in done["attempts"]] == [
        "inventory",
        "fraud",
        "gift_wrap",
        "shipping",
    ]
    assert done["attempts"][2]["stage_output"]["wrapped"] is True
