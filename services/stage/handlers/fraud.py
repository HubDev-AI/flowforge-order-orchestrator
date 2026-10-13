from typing import Any

from contracts import Command
from stage_kit import StageHandler

SCORE_BUCKETS = 100


class Handler(StageHandler):
    async def execute(self, command: Command) -> dict[str, Any]:
        return {"score": (command.order_id.int % SCORE_BUCKETS) / SCORE_BUCKETS}
