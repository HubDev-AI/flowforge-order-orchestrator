import pytest
from orchestrator.app.lifespan import lifespan
from orchestrator.app.main import create_app
from pydantic import ValidationError

PIPELINE = """
stages = [{stages}]

[defaults]
max_attempts = 3
backoff_base_seconds = 1.0
backoff_cap_seconds = 30.0
deadline_seconds = 10.0
max_wait_seconds = 60.0

[adapters]
bus = "memory"
store = "memory"
"""


def write_pipeline(tmp_path, monkeypatch, stages: str):
    path = tmp_path / "pipeline.toml"
    path.write_text(PIPELINE.format(stages=stages))
    monkeypatch.setenv("PIPELINE_CONFIG", str(path))


async def test_startup_fails_when_the_pipeline_names_a_stage_twice(tmp_path, monkeypatch):
    write_pipeline(tmp_path, monkeypatch, '"inventory", "fraud", "fraud"')

    with pytest.raises(ValidationError, match="duplicate stage: fraud"):
        async with lifespan(create_app(with_lifespan=False)):
            pass
