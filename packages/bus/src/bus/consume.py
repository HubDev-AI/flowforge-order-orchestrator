import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

RESTART_DELAY_SECONDS = 1.0


async def consume_forever[T](
    open_stream: Callable[[], AsyncIterator[T]], handle: Callable[[T], Awaitable[object]]
) -> None:
    while True:
        try:
            async for item in open_stream():
                try:
                    await handle(item)
                except Exception:
                    logger.exception("handling a %s failed", type(item).__name__)
        except Exception:
            logger.exception("consumer stopped; restarting")
        await asyncio.sleep(RESTART_DELAY_SECONDS)
