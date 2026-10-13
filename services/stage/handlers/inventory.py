import logging
from typing import Any

from contracts import Command
from stage_kit import StageHandler

logger = logging.getLogger(__name__)


class Handler(StageHandler):
    async def execute(self, command: Command) -> dict[str, Any]:
        return {"reservation_id": f"res-{command.order_id}"}

    async def undo(self, command: Command) -> None:
        reservation_id = (command.prior_stage_output or {}).get("reservation_id")
        logger.info("released reservation %s of order %s", reservation_id, command.order_id)
