import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.adapters.postgres_store import PostgresStore
from orchestrator.app.clock import Clock, utc_now
from orchestrator.app.config import PipelineConfig, Settings, load_pipeline_config
from orchestrator.app.workflow import Launcher, celery_launcher
from orchestrator.domain.models import Order
from orchestrator.ports.store import OrderStore

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    config: PipelineConfig
    store: OrderStore
    launch: Launcher
    clock: Clock


def _required(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"{name} is required by the configured adapter")
    return value


async def _launch_nothing(order: Order) -> None:
    logger.info("order %s accepted; the memory bus runs no stages", order.id)


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
        launch = celery_launcher(_required(settings.rabbitmq_url, "RABBITMQ_URL"), config)
    else:
        launch = _launch_nothing
    return Runtime(config, store, launch, utc_now)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    config = load_pipeline_config(settings.pipeline_config)
    async with AsyncExitStack() as stack:
        app.state.runtime = await _open_runtime(settings, config, stack)
        yield
