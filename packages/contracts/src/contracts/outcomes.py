from enum import StrEnum


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRY = "retry"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"
    EXHAUSTED = "exhausted"


class CommandKind(StrEnum):
    EXECUTE = "execute"
    UNDO = "undo"
