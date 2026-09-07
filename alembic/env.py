"""Alembic migration environment.

Reads the DB URL from application settings (``app.core.config``) rather
than duplicating it in ``alembic.ini``, so local dev/staging/production all
resolve to the same env-driven connection string. Runs migrations using an
async engine since the app itself is async (SQLAlchemy 2.0 + asyncpg).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401 - registers all model tables on Base.metadata for autogenerate
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL, no live DB connection)."""
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live async connection."""
    async_engine: AsyncEngine = engine
    async with async_engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await async_engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
