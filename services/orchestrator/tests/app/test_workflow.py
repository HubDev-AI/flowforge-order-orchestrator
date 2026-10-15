from datetime import timedelta

from contracts import STAGE_ACTOR, CommandKind
from dramatiq.brokers.stub import StubBroker
from orchestrator.app.workflow import GIVE_UP_MARGIN, gave_up_event, order_pipeline, plan_for
from orchestrator.domain.models import OrderState

from .conftest import pipeline_config


def test_the_pipeline_runs_each_stage_on_its_own_queue_with_its_policy(make_order):
    config = pipeline_config().model_copy(
        update={"overrides": {"fraud": {"deadline_seconds": 5, "max_attempts": 6}}}
    )
    order = make_order(payload={"payload": {"sku": "A-1"}})

    messages = order_pipeline(StubBroker(), order, config).messages

    assert {m.actor_name for m in messages} == {STAGE_ACTOR}
    assert [m.queue_name for m in messages] == ["stage.inventory", "stage.fraud", "stage.shipping"]
    assert [m.options["max_retries"] for m in messages] == [2, 5, 2]
    assert [m.options["time_limit"] for m in messages] == [15000, 10000, 15000]
    assert [m.kwargs["position"] for m in messages] == [0, 1, 2]
    assert {m.kwargs["kind"] for m in messages} == {"execute"}


def test_each_stage_hands_over_to_the_next_and_the_last_ends_the_pipeline(make_order):
    messages = order_pipeline(StubBroker(), make_order(), pipeline_config()).messages

    targets = [m.options.get("pipe_target") for m in messages]
    assert [t["queue_name"] if t else None for t in targets] == [
        "stage.fraud",
        "stage.shipping",
        None,
    ]
    assert all(m.options["pipe_ignore"] for m in messages)


def test_the_plan_snapshots_the_order_stages_with_their_policies(make_order):
    order = make_order(stages=("inventory", "shipping"))

    plan = plan_for(order, pipeline_config())

    assert [step["name"] for step in plan] == ["inventory", "shipping"]
    assert plan[0]["policy"] == {
        "max_attempts": 3,
        "backoff_base_seconds": 1.0,
        "backoff_cap_seconds": 30.0,
        "deadline_seconds": 10.0,
        "max_wait_seconds": 60.0,
    }


def test_an_order_is_given_up_only_after_its_stage_max_wait_and_the_margin(make_order, now):
    config = pipeline_config()
    order = make_order(
        state=OrderState.RUNNING,
        current_stage="fraud",
        current_kind=CommandKind.EXECUTE,
        waiting_since=now,
    )
    limit = timedelta(seconds=60) + GIVE_UP_MARGIN

    assert gave_up_event(order, config, now + limit) is None
    event = gave_up_event(order, config, now + limit + timedelta(seconds=1))

    assert (event["type"], event["position"], event["kind"]) == ("gave_up", 1, "execute")
    assert event["waiting_since"] == now.isoformat()


def test_a_pending_order_is_given_up_at_its_first_stage(make_order, now):
    order = make_order(waiting_since=now)

    event = gave_up_event(order, pipeline_config(), now + timedelta(minutes=5))

    assert (event["position"], event["kind"]) == (0, "execute")


def test_a_compensating_order_is_given_up_on_its_undo(make_order, now):
    order = make_order(
        state=OrderState.COMPENSATING,
        current_stage="inventory",
        current_kind=CommandKind.UNDO,
        waiting_since=now,
    )

    event = gave_up_event(order, pipeline_config(), now + timedelta(minutes=5))

    assert (event["position"], event["kind"]) == (0, "undo")
