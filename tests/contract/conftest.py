import socket
from pathlib import Path

import aio_pika
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from bus import RESULTS_QUEUE, MemoryBus, RabbitMQBus, stage_queue
from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.adapters.postgres_store import PostgresStore
from orchestrator.domain.transitions import plan_dispatch, transition
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

ALEMBIC_INI = Path(__file__).parents[2] / "services" / "orchestrator" / "alembic.ini"
POSTGRES_IMAGE = "postgres:18.6-alpine"
RABBITMQ_IMAGE = "rabbitmq:4.3.6-management"
RABBITMQ_CREDENTIALS = "flowforge"
DECLARED_STAGES = ["inventory", "fraud"]


def run_rabbitmq(host_port: int | None = None) -> DockerContainer:
    container = (
        DockerContainer(RABBITMQ_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", RABBITMQ_CREDENTIALS)
        .with_env("RABBITMQ_DEFAULT_PASS", RABBITMQ_CREDENTIALS)
        .with_exposed_ports(5672)
        .waiting_for(LogMessageWaitStrategy("Server startup complete"))
    )
    if host_port is not None:
        container.with_bind_ports(5672, host_port)
    return container


def amqp_url(container: DockerContainer) -> str:
    host, port = container.get_container_host_ip(), container.get_exposed_port(5672)
    return f"amqp://{RABBITMQ_CREDENTIALS}:{RABBITMQ_CREDENTIALS}@{host}:{port}/"


def _upgrade(connection) -> None:
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container


@pytest.fixture
def upgrade_to_head():
    async def upgrade(url: str) -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
        await engine.dispose()

    return upgrade


@pytest.fixture
def fresh_postgres(postgres_container, upgrade_to_head):
    async def build() -> AsyncEngine:
        url = postgres_container.get_connection_url()
        await upgrade_to_head(url)
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.execute(sa.text("TRUNCATE orders, attempts"))
        return engine

    return build


@pytest.fixture(
    params=["memory", pytest.param("postgres", marks=pytest.mark.contract)],
)
async def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    engine = await request.getfixturevalue("fresh_postgres")()
    yield PostgresStore(engine)
    await engine.dispose()


@pytest.fixture
def dispatch(now, policy):
    return lambda order: plan_dispatch(order, now, lambda stage: policy)


@pytest.fixture
def settle_with(policy):
    def build(event_for):
        return lambda order, attempt: transition(order, event_for(attempt), policy)

    return build


@pytest.fixture(scope="session")
def rabbitmq_container():
    with run_rabbitmq() as container:
        yield container


@pytest.fixture
def rabbitmq_bus_factory(request, now):
    async def build() -> RabbitMQBus:
        url = amqp_url(request.getfixturevalue("rabbitmq_container"))
        connection = await aio_pika.connect_robust(url)
        async with connection:
            channel = await connection.channel()
            for name in [
                *map(stage_queue, DECLARED_STAGES),
                RESULTS_QUEUE,
            ]:
                queue = await channel.declare_queue(name, durable=True)
                await queue.purge()
        bus = await RabbitMQBus.connect(url, clock=lambda: now)
        await bus.declare_topology(DECLARED_STAGES)
        return bus

    return build


@pytest.fixture(params=["memory", pytest.param("rabbitmq", marks=pytest.mark.contract)])
async def bus(request, now):
    if request.param == "memory":
        bus = MemoryBus(clock=lambda: now)
        await bus.declare_topology(DECLARED_STAGES)
    else:
        bus = await request.getfixturevalue("rabbitmq_bus_factory")()
    yield bus
    await bus.close()


@pytest.fixture
def amqp_url_of():
    return amqp_url


@pytest.fixture
def new_rabbitmq():
    return run_rabbitmq


@pytest.fixture
def free_port():
    def find() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    return find
