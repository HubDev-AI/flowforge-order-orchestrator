from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import ALEMBIC_INI

pytestmark = pytest.mark.contract

ORDER = (
    "INSERT INTO orders (id, idempotency_key, payload, payload_hash, stages, state,"
    " current_stage, current_kind, created_at, updated_at) VALUES (:id, :key, '{}', 'h',"
    " '[\"inventory\"]', :state, 'inventory', :current_kind, now(), now())"
)
ATTEMPT = (
    "INSERT INTO attempts (order_id, stage, kind, attempt, outcome, deadline_at, started_at)"
    " VALUES (:order_id, 'inventory', :kind, 1, :outcome, now(), now())"
)


@pytest.fixture
def probe_database(postgres_container):
    async def create(name: str) -> str:
        admin = create_async_engine(
            postgres_container.get_connection_url(), isolation_level="AUTOCOMMIT"
        )
        async with admin.connect() as connection:
            await connection.execute(sa.text(f"DROP DATABASE IF EXISTS {name}"))
            await connection.execute(sa.text(f"CREATE DATABASE {name}"))
        await admin.dispose()
        return postgres_container.get_connection_url().rsplit("/", 1)[0] + f"/{name}"

    return create


async def migrate(url: str, action, revision: str) -> None:
    def run(connection) -> None:
        config = Config(str(ALEMBIC_INI))
        config.attributes["connection"] = connection
        action(config, revision)

    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(run)
    await engine.dispose()


async def test_migration_applies_to_an_empty_database_and_reruns_cleanly(
    probe_database, upgrade_to_head
):
    probe_url = await probe_database("migration_probe")

    await upgrade_to_head(probe_url)
    await upgrade_to_head(probe_url)

    engine = create_async_engine(probe_url)
    async with engine.connect() as connection:
        tables = (
            await connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                )
            )
        ).scalars()
        revisions = (
            await connection.execute(sa.text("SELECT count(*) FROM alembic_version"))
        ).scalar()
    await engine.dispose()
    assert {"orders", "attempts"} <= set(tables)
    assert revisions == 1


@pytest.mark.parametrize(
    ("statement", "values"),
    [
        (ORDER, {"state": "paused", "current_kind": None}),
        (ORDER, {"state": "running", "current_kind": "rollback"}),
        (ATTEMPT, {"kind": "rollback", "outcome": None}),
        (ATTEMPT, {"kind": "execute", "outcome": "lost"}),
    ],
    ids=["orders.state", "orders.current_kind", "attempts.kind", "attempts.outcome"],
)
async def test_the_schema_rejects_a_value_outside_its_enum(
    probe_database, upgrade_to_head, statement, values
):
    url = await probe_database("enum_probe")
    await upgrade_to_head(url)
    engine = create_async_engine(url)
    order_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(ORDER), {"id": order_id, "key": "k", "state": "running", "current_kind": None}
        )

    with pytest.raises(IntegrityError, match="ck_"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(statement), {"id": uuid4(), "key": "k2", "order_id": order_id, **values}
            )
    await engine.dispose()


async def test_the_enum_checks_upgrade_a_populated_database_and_downgrade_without_data_loss(
    probe_database,
):
    url = await probe_database("downgrade_probe")
    await migrate(url, command.upgrade, "0001")
    engine = create_async_engine(url)
    order_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(ORDER),
            {"id": order_id, "key": "k", "state": "compensating", "current_kind": "undo"},
        )
        await connection.execute(
            sa.text(ATTEMPT), {"order_id": order_id, "kind": "execute", "outcome": None}
        )

    async def rows():
        async with engine.connect() as connection:
            return (
                (await connection.execute(sa.text("SELECT state, current_kind FROM orders"))).all(),
                (await connection.execute(sa.text("SELECT kind, outcome FROM attempts"))).all(),
            )

    before = await rows()
    await migrate(url, command.upgrade, "0002")
    upgraded = await rows()
    await migrate(url, command.downgrade, "0001")
    downgraded = await rows()
    async with engine.connect() as connection:
        checks = await connection.scalar(
            sa.text("SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck_%'")
        )
    await engine.dispose()

    assert before == upgraded == downgraded
    assert checks == 0
