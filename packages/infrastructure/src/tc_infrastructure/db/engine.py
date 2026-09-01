"""Async engine and session construction.

The application connects as the least-privilege role, which holds no UPDATE or
DELETE on ``thoughts``. Migrations use a separate role and a separate,
synchronous engine (see ``migrations/env.py``); they never run from process
startup (docs/DESIGN.md 13).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tc_infrastructure.config import Settings

# Bound so that an unreachable database fails in seconds rather than hanging a
# capture. Discord acknowledgement has a five-second p95 budget
# (docs/DESIGN.md 2.1); a connection that has not arrived by then is a failure.
CONNECT_TIMEOUT_SECONDS = 5


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the application's async engine from configuration."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
        # Do not echo SQL: statements carry raw thought bodies, which must not
        # reach normal logs (docs/DESIGN.md 14.2).
        echo=False,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with autoflush off, so writes happen where we say they do."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


__all__ = ["AsyncSession", "create_engine", "create_session_factory"]
