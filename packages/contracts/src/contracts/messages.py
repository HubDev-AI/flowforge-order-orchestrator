from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from contracts.outcomes import CommandKind, Outcome
from contracts.script import OutcomeScript


class AttemptKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: UUID
    stage: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    kind: CommandKind


class Command(AttemptKey):
    deadline_at: AwareDatetime
    payload: dict[str, Any]
    script: OutcomeScript = Field(default_factory=OutcomeScript)
    prior_stage_output: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _only_undo_carries_prior_output(self) -> Self:
        if self.kind is CommandKind.EXECUTE and self.prior_stage_output is not None:
            raise ValueError("prior_stage_output is only valid on an undo command")
        return self


class Result(AttemptKey):
    outcome: Outcome
    reason: str | None = None
    stage_output: dict[str, Any] | None = None
