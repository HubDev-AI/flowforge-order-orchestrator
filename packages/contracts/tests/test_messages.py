from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from contracts import Command, CommandKind, Outcome, OutcomeScript
from pydantic import ValidationError

ORDER_ID = uuid4()


def make_command(**overrides):
    fields = {
        "order_id": ORDER_ID,
        "stage": "fraud",
        "attempt": 1,
        "kind": CommandKind.EXECUTE,
        "deadline_at": datetime(2026, 10, 13, 12, 0, 10, tzinfo=UTC),
        "payload": {"sku": "A-1", "qty": 2},
    }
    return Command(**(fields | overrides))


def test_command_json_round_trip_keeps_deadline_timezone():
    deadline = datetime(2026, 10, 13, 12, 0, 10, tzinfo=timezone(timedelta(hours=2)))
    command = make_command(
        deadline_at=deadline,
        script=OutcomeScript.model_validate({"fraud": {"execute": ["retry", "succeed"]}}),
    )

    parsed = Command.model_validate_json(command.model_dump_json())

    assert parsed == command
    assert parsed.deadline_at.utcoffset() == timedelta(hours=2)


def test_command_rejects_naive_deadline():
    with pytest.raises(ValidationError, match="deadline_at"):
        make_command(deadline_at=datetime(2026, 10, 13, 12, 0, 10))


def test_undo_command_carries_the_execute_stage_output():
    undo = make_command(kind=CommandKind.UNDO, prior_stage_output={"reservation_id": "r-1"})

    assert Command.model_validate_json(undo.model_dump_json()).prior_stage_output == {
        "reservation_id": "r-1"
    }


def test_execute_command_carries_no_prior_stage_output():
    assert make_command().prior_stage_output is None
    with pytest.raises(ValidationError, match="prior_stage_output"):
        make_command(prior_stage_output={"reservation_id": "r-1"})


def test_outcomes_match_the_status_contract():
    assert {outcome.value for outcome in Outcome} == {
        "succeeded",
        "retry",
        "timed_out",
        "rejected",
        "exhausted",
    }
