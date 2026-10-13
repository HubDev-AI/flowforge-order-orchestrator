import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from orchestrator.domain.models import StagePolicy


class Settings(BaseSettings):
    database_url: str | None = Field(default=None, min_length=1)
    rabbitmq_url: str | None = Field(default=None, min_length=1)
    pipeline_config: Path = Path("config/pipeline.toml")


class StageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(ge=1)
    backoff_base_seconds: float = Field(gt=0)
    backoff_cap_seconds: float = Field(gt=0)
    dispatch_deadline_seconds: float = Field(gt=0)


class SweepSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: float = Field(gt=0)
    pending_threshold_seconds: float = Field(ge=0)


class AdapterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bus: Literal["rabbitmq", "memory"]
    store: Literal["postgres", "memory"]


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[str] = Field(min_length=1)
    defaults: StageSettings
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sweep: SweepSettings
    adapters: AdapterSettings

    @field_validator("stages")
    @classmethod
    def _stages_are_unique(cls, stages: list[str]) -> list[str]:
        for index, stage in enumerate(stages):
            if stage in stages[:index]:
                raise ValueError(f"duplicate stage: {stage}")
        return stages

    @model_validator(mode="after")
    def _overrides_are_valid(self) -> Self:
        for stage in self.overrides:
            if stage not in self.stages:
                raise ValueError(f"override for unknown stage: {stage}")
            self._settings_for(stage)
        return self

    def policy_for(self, stage: str) -> StagePolicy:
        settings = self._settings_for(stage)
        return StagePolicy(
            max_attempts=settings.max_attempts,
            backoff_base=timedelta(seconds=settings.backoff_base_seconds),
            backoff_cap=timedelta(seconds=settings.backoff_cap_seconds),
            dispatch_deadline=timedelta(seconds=settings.dispatch_deadline_seconds),
        )

    def _settings_for(self, stage: str) -> StageSettings:
        return StageSettings.model_validate(
            self.defaults.model_dump() | self.overrides.get(stage, {})
        )


def load_pipeline_config(path: Path) -> PipelineConfig:
    with path.open("rb") as file:
        return PipelineConfig.model_validate(tomllib.load(file))
