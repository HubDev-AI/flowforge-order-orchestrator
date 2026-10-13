from typing import Any

from contracts import Command
from stage_kit import StageHandler


class Handler(StageHandler):
    async def execute(self, command: Command) -> dict[str, Any]:
        return {"label_id": f"lbl-{command.order_id}"}
