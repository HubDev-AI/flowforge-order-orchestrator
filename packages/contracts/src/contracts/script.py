from collections.abc import Callable, Collection
from enum import StrEnum

from pydantic import Field, RootModel

from contracts.outcomes import CommandKind


class ScriptToken(StrEnum):
    SUCCEED = "succeed"
    RETRY = "retry"
    REJECT = "reject"
    TIMEOUT = "timeout"


class OutcomeScript(RootModel[dict[str, dict[CommandKind, list[ScriptToken]]]]):
    root: dict[str, dict[CommandKind, list[ScriptToken]]] = Field(default_factory=dict)

    def validate_stages(self, stages: Collection[str]) -> None:
        unknown = sorted(set(self.root) - set(stages))
        if unknown:
            raise ValueError(f"script names unknown stage: {', '.join(unknown)}")

    def validate_lengths(self, max_attempts_for: Callable[[str], int]) -> None:
        for stage, by_kind in self.root.items():
            cap = max_attempts_for(stage)
            for kind, tokens in by_kind.items():
                if len(tokens) > cap:
                    raise ValueError(
                        f"script lists {len(tokens)} {kind} tokens for {stage}; "
                        f"the stage allows {cap} attempts"
                    )
