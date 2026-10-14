import pytest
from orchestrator import recorder

POLICY = {
    "max_attempts": 3,
    "backoff_base_seconds": 1.0,
    "backoff_cap_seconds": 30.0,
    "deadline_seconds": 10.0,
}
PLAN = [{"name": name, "policy": POLICY} for name in ("inventory", "fraud", "shipping")]


@pytest.fixture
def launched(monkeypatch):
    pipelines = []

    class Captured:
        def __init__(self, messages, broker=None):
            self.messages = messages

        def run(self):
            pipelines.append(self.messages)

    monkeypatch.setattr(recorder.dramatiq, "pipeline", Captured)
    return pipelines


def test_compensation_undoes_every_dispatched_stage_in_reverse_with_its_output(launched):
    recorder.compensate("o-1", PLAN, 1, {"payload": {}}, {0: {"reservation_id": "r-1"}, 1: None})

    [messages] = launched
    assert [(m.queue_name, m.kwargs["kind"]) for m in messages] == [
        ("stage.fraud", "undo"),
        ("stage.inventory", "undo"),
    ]
    assert [m.kwargs["prior_output"] for m in messages] == [None, {"reservation_id": "r-1"}]
