"""Async SQLAlchemy engine/session factory.

Shared by the FastAPI app (via a future ``get_db`` dependency added by
``backend-builder``) and by Alembic's ``env.py`` for migrations.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_settings = get_settings()

engine: AsyncEngine = create_async_engine(_settings.database_url, echo=False, future=True)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an ``AsyncSession`` per request."""
    async with async_session_factory() as session:
        yield session
