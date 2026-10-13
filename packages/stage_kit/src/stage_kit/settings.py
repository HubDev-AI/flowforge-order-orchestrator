from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from stage_kit.simulator import Profile

STAGE_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class StageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAGE_")

    name: str = Field(pattern=STAGE_NAME_PATTERN)
    rabbitmq_url: str = Field(min_length=1, validation_alias="RABBITMQ_URL")
    latency_min_s: float = Field(ge=0)
    latency_max_s: float = Field(ge=0)
    transient_probability: float = Field(ge=0, le=1)
    reject_probability: float = Field(ge=0, le=1)
    seed: int | None = None

    @model_validator(mode="after")
    def _profile_is_consistent(self) -> Self:
        if self.latency_min_s > self.latency_max_s:
            raise ValueError("latency_min_s must not exceed latency_max_s")
        if self.transient_probability + self.reject_probability > 1:
            raise ValueError("transient and reject probabilities must sum to at most 1")
        return self

    @property
    def profile(self) -> Profile:
        return Profile(
            self.latency_min_s,
            self.latency_max_s,
            self.transient_probability,
            self.reject_probability,
        )
