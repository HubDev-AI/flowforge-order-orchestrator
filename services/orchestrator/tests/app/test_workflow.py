from celery import Celery
from contracts import COMPENSATE_TASK, STAGE_TASK
from orchestrator.app.workflow import order_chain, plan_for

from .conftest import pipeline_config


def test_the_chain_runs_each_stage_on_its_own_queue_with_its_deadline(make_order):
    config = pipeline_config().model_copy(
        update={"overrides": {"fraud": {"dispatch_deadline_seconds": 5, "max_attempts": 6}}}
    )
    order = make_order(payload={"payload": {"sku": "A-1"}})

    steps = order_chain(Celery(), order, config).tasks

    assert [s.task for s in steps] == [STAGE_TASK] * 3
    assert [s.options["queue"] for s in steps] == [
        "stage.inventory",
        "stage.fraud",
        "stage.shipping",
    ]
    assert [s.options["soft_time_limit"] for s in steps] == [10.0, 5.0, 10.0]
    assert all(s.immutable for s in steps)
    assert [s.kwargs["position"] for s in steps] == [0, 1, 2]
    assert {s.kwargs["kind"] for s in steps} == {"execute"}


def test_every_step_carries_the_compensation_errback(make_order):
    steps = order_chain(Celery(), make_order(), pipeline_config()).unchain_tasks()

    assert all(
        [errback["task"] for errback in s.options["link_error"]] == [COMPENSATE_TASK] for s in steps
    )


def test_the_plan_snapshots_the_order_stages_with_their_policies(make_order):
    order = make_order(stages=("inventory", "shipping"))

    plan = plan_for(order, pipeline_config())

    assert [step["name"] for step in plan] == ["inventory", "shipping"]
    assert plan[0]["policy"] == {
        "max_attempts": 3,
        "backoff_base_seconds": 1.0,
        "backoff_cap_seconds": 30.0,
        "deadline_seconds": 10.0,
    }
