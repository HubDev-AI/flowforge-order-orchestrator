import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.contract


async def test_migration_applies_to_an_empty_database_and_reruns_cleanly(
    postgres_container, upgrade_to_head
):
    admin = create_async_engine(
        postgres_container.get_connection_url(), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as connection:
        await connection.execute(sa.text("CREATE DATABASE migration_probe"))
    await admin.dispose()
    probe_url = postgres_container.get_connection_url().rsplit("/", 1)[0] + "/migration_probe"

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
