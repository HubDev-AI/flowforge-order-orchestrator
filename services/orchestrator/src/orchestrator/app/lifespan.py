import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from bus import MemoryBus, MessageBus, RabbitMQBus
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.adapters.postgres_store import PostgresStore
from orchestrator.app.clock import Clock, utc_now
from orchestrator.app.config import PipelineConfig, Settings, load_pipeline_config
from orchestrator.app.dispatcher import Dispatcher
from orchestrator.app.results import ResultsConsumer
from orchestrator.ports.store import OrderStore

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    config: PipelineConfig
    store: OrderStore
    bus: MessageBus
    clock: Clock
    nudge: asyncio.Event


def _required(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"{name} is required by the configured adapter")
    return value


async def _open_runtime(
    settings: Settings, config: PipelineConfig, stack: AsyncExitStack
) -> Runtime:
    if config.adapters.store == "postgres":
        engine = create_async_engine(_required(settings.database_url, "DATABASE_URL"))
        stack.push_async_callback(engine.dispose)
        store: OrderStore = PostgresStore(engine)
    else:
        store = MemoryStore()
    if config.adapters.bus == "rabbitmq":
        bus: MessageBus = await RabbitMQBus.connect(
            _required(settings.rabbitmq_url, "RABBITMQ_URL")
        )
    else:
        bus = MemoryBus()
    stack.push_async_callback(bus.close)
    await bus.declare_topology(config.stages)
    return Runtime(config, store, bus, utc_now, asyncio.Event())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    config = load_pipeline_config(settings.pipeline_config)
    async with AsyncExitStack() as stack:
        runtime = await _open_runtime(settings, config, stack)
        app.state.runtime = runtime
        dispatcher = Dispatcher(runtime.store, runtime.bus, config, runtime.clock, runtime.nudge)
        consumer = ResultsConsumer(runtime.store, runtime.bus, config, runtime.clock, runtime.nudge)
        tasks = [asyncio.create_task(dispatcher.run()), asyncio.create_task(consumer.run())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
