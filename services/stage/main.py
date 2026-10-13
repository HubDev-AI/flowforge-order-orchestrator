import asyncio
import importlib
import logging
import random

from bus import RabbitMQBus
from stage_kit import Simulator, StageHandler, StageRunner, StageSettings


class UnknownStage(Exception):
    pass


def load_handler(name: str) -> StageHandler:
    try:
        module = importlib.import_module(f"handlers.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"handlers.{name}":
            raise
        raise UnknownStage(f"unknown stage {name!r}: there is no handlers/{name}.py") from error
    return module.Handler()


async def main() -> None:
    settings = StageSettings()
    handler = load_handler(settings.name)
    bus = await RabbitMQBus.connect(settings.rabbitmq_url)
    simulator = Simulator(settings.profile, random.Random(settings.seed))
    try:
        await StageRunner(settings.name, handler, simulator, bus).run()
    finally:
        await bus.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
