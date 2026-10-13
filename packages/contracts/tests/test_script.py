import pytest
from contracts import CommandKind, OutcomeScript, ScriptToken
from pydantic import ValidationError


def test_script_with_timeout_token_parses():
    script = OutcomeScript.model_validate({"fraud": {"execute": ["succeed", "timeout"]}})

    assert script.root["fraud"][CommandKind.EXECUTE][1] is ScriptToken.TIMEOUT


def test_unknown_token_is_rejected_with_stage_and_index():
    with pytest.raises(ValidationError) as error:
        OutcomeScript.model_validate({"fraud": {"execute": ["succeed", "explode"]}})

    assert error.value.errors()[0]["loc"] == ("fraud", "execute", 1)


def test_script_for_a_stage_outside_the_list_is_rejected():
    script = OutcomeScript.model_validate({"shipping": {"execute": ["succeed"]}})

    with pytest.raises(ValueError, match="shipping"):
        script.validate_stages(["inventory", "fraud"])


def test_script_for_listed_stages_passes_stage_validation():
    script = OutcomeScript.model_validate({"fraud": {"undo": ["retry"]}})

    script.validate_stages(["inventory", "fraud"])


def test_script_round_trips_through_json():
    script = OutcomeScript.model_validate(
        {"fraud": {"execute": ["retry", "reject"], "undo": ["succeed"]}}
    )

    assert OutcomeScript.model_validate_json(script.model_dump_json()) == script


def test_a_script_longer_than_the_stage_cap_is_rejected_with_the_counts():
    script = OutcomeScript.model_validate({"fraud": {"undo": ["retry", "retry"]}})

    with pytest.raises(ValueError, match="2 undo tokens for fraud; the stage allows 1 attempts"):
        script.validate_lengths(lambda stage: 1)


def test_a_script_within_every_stage_cap_passes():
    script = OutcomeScript.model_validate({"fraud": {"execute": ["retry", "succeed"]}})

    script.validate_lengths(lambda stage: 2)
