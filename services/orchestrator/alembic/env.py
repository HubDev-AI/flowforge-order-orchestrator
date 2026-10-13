import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config


def run_migrations(connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_on_new_engine() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


shared_connection = config.attributes.get("connection")
if shared_connection is None:
    asyncio.run(run_migrations_on_new_engine())
else:
    run_migrations(shared_connection)
