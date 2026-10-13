from typing import Any

from contracts import Command
from stage_kit import StageHandler


class Handler(StageHandler):
    async def execute(self, command: Command) -> dict[str, Any]:
        return {"wrapped": True, "wrap_id": f"wrap-{command.order_id}"}
