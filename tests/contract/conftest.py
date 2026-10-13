from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.adapters.postgres_store import PostgresStore
from orchestrator.domain.transitions import plan_dispatch, transition
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

ALEMBIC_INI = Path(__file__).parents[2] / "services" / "orchestrator" / "alembic.ini"
POSTGRES_IMAGE = "postgres:18.6-alpine"


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
