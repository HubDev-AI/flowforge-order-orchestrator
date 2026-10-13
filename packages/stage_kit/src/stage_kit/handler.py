from abc import ABC, abstractmethod
from typing import Any

from contracts import Command


class StageHandler(ABC):
    @abstractmethod
    async def execute(self, command: Command) -> dict[str, Any] | None: ...

    async def undo(self, command: Command) -> dict[str, Any] | None:
        return None
