from datetime import timedelta
from pathlib import Path

import pytest
from orchestrator.app.config import PipelineConfig, load_pipeline_config
from pydantic import ValidationError

SHIPPED_CONFIG = Path(__file__).parents[4] / "config" / "pipeline.toml"

BASE = {
    "stages": ["inventory", "fraud", "shipping"],
    "defaults": {
        "max_attempts": 3,
        "backoff_base_seconds": 1.0,
        "backoff_cap_seconds": 30.0,
        "dispatch_deadline_seconds": 10.0,
    },
    "sweep": {"interval_seconds": 1.0, "pending_threshold_seconds": 0.0},
    "adapters": {"bus": "memory", "store": "memory"},
}


def test_the_shipped_pipeline_file_loads():
    config = load_pipeline_config(SHIPPED_CONFIG)

    assert config.stages == ["inventory", "fraud", "shipping"]
    assert (config.adapters.bus, config.adapters.store) == ("rabbitmq", "postgres")


def test_a_duplicate_stage_is_rejected_and_named():
    with pytest.raises(ValidationError, match="duplicate stage: fraud"):
        PipelineConfig.model_validate({**BASE, "stages": ["inventory", "fraud", "fraud"]})


def test_an_empty_stage_list_is_rejected():
    with pytest.raises(ValidationError, match="stages"):
        PipelineConfig.model_validate({**BASE, "stages": []})


def test_a_stage_override_replaces_only_the_keys_it_names():
    config = PipelineConfig.model_validate(
        {**BASE, "overrides": {"fraud": {"max_attempts": 6, "dispatch_deadline_seconds": 5}}}
    )

    fraud, inventory = config.policy_for("fraud"), config.policy_for("inventory")

    assert (fraud.max_attempts, fraud.dispatch_deadline) == (6, timedelta(seconds=5))
    assert fraud.backoff_base == timedelta(seconds=1)
    assert (inventory.max_attempts, inventory.dispatch_deadline) == (3, timedelta(seconds=10))


def test_a_stage_that_left_the_pipeline_still_has_a_policy():
    retired = PipelineConfig.model_validate(BASE).policy_for("retired")

    assert retired.max_attempts == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"gift_wrap": {"max_attempts": 2}},
        {"fraud": {"max_attempt": 2}},
        {"fraud": {"max_attempts": 0}},
    ],
)
def test_an_override_for_an_unknown_stage_key_or_value_is_rejected(overrides):
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate({**BASE, "overrides": overrides})


def test_an_unknown_top_level_key_is_rejected():
    with pytest.raises(ValidationError, match="surprise"):
        PipelineConfig.model_validate({**BASE, "surprise": 1})
