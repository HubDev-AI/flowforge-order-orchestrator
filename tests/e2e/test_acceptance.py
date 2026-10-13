import httpx
import pytest

pytestmark = pytest.mark.e2e

EXECUTE, UNDO = "execute", "undo"


def test_resubmitting_a_key_returns_the_order_and_a_changed_payload_is_a_conflict(orders):
    first = orders.submit(payload={"sku": "A-1"}, key="reuse-1")
    replay = orders.submit(payload={"sku": "A-1"}, key="reuse-1")
    conflict = orders.submit(payload={"sku": "B-2"}, key="reuse-1")

    assert (first.status_code, replay.status_code, conflict.status_code) == (201, 200, 409)
    assert replay.json()["id"] == first.json()["id"]
    done = orders.finish(first.json()["id"])
    assert done["state"] == "completed"
    assert len(done["attempts"]) == 3


def test_an_order_runs_every_stage_and_completes(orders):
    order_id = orders.start()

    done = orders.finish(order_id)

    assert done["state"] == "completed"
    assert orders.history(done) == [
        ("inventory", EXECUTE, 1, "succeeded"),
        ("fraud", EXECUTE, 1, "succeeded"),
        ("shipping", EXECUTE, 1, "succeeded"),
    ]
    assert [a["stage_output"] is not None for a in done["attempts"]] == [True] * 3


def test_scripted_transient_failures_are_retried_until_the_stage_succeeds(orders):
    order_id = orders.start({"fraud": {"execute": ["retry", "retry", "succeed"]}})

    done = orders.finish(order_id)

    assert done["state"] == "completed"
    assert orders.history(done) == [
        ("inventory", EXECUTE, 1, "succeeded"),
        ("fraud", EXECUTE, 1, "retry"),
        ("fraud", EXECUTE, 2, "retry"),
        ("fraud", EXECUTE, 3, "succeeded"),
        ("shipping", EXECUTE, 1, "succeeded"),
    ]


def test_a_stage_that_exhausts_its_attempts_is_undone_and_the_order_fails(orders):
    order_id = orders.start({"inventory": {"execute": ["retry", "retry", "retry"]}})

    done = orders.finish(order_id)

    assert done["state"] == "failed"
    assert orders.history(done) == [
        ("inventory", EXECUTE, 1, "retry"),
        ("inventory", EXECUTE, 2, "retry"),
        ("inventory", EXECUTE, 3, "exhausted"),
        ("inventory", UNDO, 1, "succeeded"),
    ]
    assert "inventory exhausted" in done["terminal_reason"]


def test_a_rejection_undoes_the_failing_stage_then_the_earlier_ones_in_reverse(orders):
    order_id = orders.start({"fraud": {"execute": ["reject"]}})

    done = orders.finish(order_id)

    assert done["state"] == "failed"
    assert orders.history(done) == [
        ("inventory", EXECUTE, 1, "succeeded"),
        ("fraud", EXECUTE, 1, "rejected"),
        ("fraud", UNDO, 1, "succeeded"),
        ("inventory", UNDO, 1, "succeeded"),
    ]
    assert "fraud rejected" in done["terminal_reason"]


def test_an_undo_that_keeps_failing_ends_in_compensation_failed(orders):
    order_id = orders.start(
        {"fraud": {"execute": ["reject"]}, "inventory": {"undo": ["retry", "retry", "retry"]}}
    )

    done = orders.finish(order_id)

    assert done["state"] == "compensation_failed"
    assert orders.history(done)[-3:] == [
        ("inventory", UNDO, 1, "retry"),
        ("inventory", UNDO, 2, "retry"),
        ("inventory", UNDO, 3, "exhausted"),
    ]
    assert done["current_stage"] is None


def test_a_stage_that_answers_after_its_deadline_is_timed_out_and_retried(orders):
    order_id = orders.start({"fraud": {"execute": ["timeout"]}})

    done = orders.finish(order_id)

    assert done["state"] == "completed"
    assert orders.history(done) == [
        ("inventory", EXECUTE, 1, "succeeded"),
        ("fraud", EXECUTE, 1, "timed_out"),
        ("fraud", EXECUTE, 2, "succeeded"),
        ("shipping", EXECUTE, 1, "succeeded"),
    ]


def test_only_the_live_attempt_runs_when_a_stage_comes_back_after_timeouts(stack, orders):
    stack.compose("stop", "stage-fraud")
    try:
        order_id = orders.start()
        orders.wait_until(
            order_id,
            lambda s: (
                [a["outcome"] for a in s["attempts"] if a["stage"] == "fraud"][:2]
                == ["timed_out", "timed_out"]
            ),
        )
    finally:
        stack.compose("start", "stage-fraud")

    done = orders.finish(order_id)

    assert done["state"] == "completed"
    fraud = [a for a in done["attempts"] if a["stage"] == "fraud"]
    assert [a["outcome"] for a in fraud] == ["timed_out", "timed_out", "succeeded"]
    lines = [line for line in stack.logs("stage-fraud").splitlines() if order_id in line]
    dropped = [line for line in lines if "dropped expired" in line]
    ran = [line for line in lines if line not in dropped]
    assert len(dropped) == 2
    assert len(ran) == 1
    assert "attempt 3: succeeded" in ran[0]


def test_the_page_for_an_unknown_order_has_its_not_found_message(stack, orders):
    unknown = "5b0e7a4e-0f0e-4d3e-9a55-0d6a9b5b5b5b"

    api = httpx.get(f"{stack.base_url}/api/orders/{unknown}")
    page = httpx.get(f"{stack.base_url}/orders/{unknown}")

    assert api.status_code == 404
    assert page.status_code == 200
    assert 'id="not-found"' in page.text
