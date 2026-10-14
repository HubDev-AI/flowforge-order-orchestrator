import time

import pytest
from contracts import STAGE_ACTOR, DropAfterMaxWait, HandOffStamp, record_message, stage_message
from dramatiq.middleware import SkipMessage

POLICY = {
    "max_attempts": 3,
    "backoff_base_seconds": 1.0,
    "backoff_cap_seconds": 30.0,
    "deadline_seconds": 10.0,
    "max_wait_seconds": 60.0,
}
PLAN = [{"name": "fraud", "policy": POLICY}]


def stage_work():
    return stage_message(order_id="o-1", position=0, kind="execute", plan=PLAN, submission={})


class Failed:
    def __init__(self, message):
        self.actor_name, self.kwargs, self.options = (
            message.actor_name,
            message.kwargs,
            message.options,
        )
        self.failed = False

    def fail(self):
        self.failed = True


def test_stage_work_is_stamped_with_when_it_becomes_available():
    message, later = stage_work(), stage_work()
    before = int(time.time() * 1000)

    HandOffStamp().before_enqueue(None, message, None)
    HandOffStamp().before_enqueue(None, later, 5000)

    assert before <= message.options["handed_off_at"] <= before + 1000
    assert later.options["handed_off_at"] - message.options["handed_off_at"] >= 4000
    assert message.actor_name == STAGE_ACTOR


def test_other_messages_are_not_stamped():
    event = record_message({"type": "started"})

    HandOffStamp().before_enqueue(None, event, None)

    assert "handed_off_at" not in event.options


def test_stage_work_that_waited_past_its_max_wait_is_dropped():
    message = Failed(stage_work())
    message.options["handed_off_at"] = int(time.time() * 1000) - 61_000

    with pytest.raises(SkipMessage):
        DropAfterMaxWait().before_process_message(None, message)

    assert message.failed


def test_stage_work_within_its_max_wait_runs():
    message = Failed(stage_work())
    message.options["handed_off_at"] = int(time.time() * 1000) - 59_000

    DropAfterMaxWait().before_process_message(None, message)

    assert not message.failed
