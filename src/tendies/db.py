"""Async database engine + session management.

Supports Postgres (production, via ``asyncpg``) and SQLite (solo testing and
the test suite, via ``aiosqlite``) behind the same SQLAlchemy async API.

The bot creates one :class:`Database` at startup; tests create their own
against an in-memory SQLite instance.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


class Database:
    """Owns the async engine and a session factory."""

    def __init__(self, url: str, *, echo: bool = False, **engine_kwargs):
        # SQLite (esp. in-memory) needs a shared connection across the async
        # session; callers pass the appropriate pool via engine_kwargs.
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, **engine_kwargs)
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def create_all(self) -> None:
        """Create any missing tables. Idempotent."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session scope that commits on success and rolls back on error.

        The whole daily tick and every multi-step economic action runs inside
        one of these so money movements are atomic — partial ticks can't leak
        or destroy nuggies.
        """
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
