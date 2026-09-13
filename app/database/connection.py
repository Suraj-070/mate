"""Database connection: async engine + session factory.

Designed to be portable between SQLite (dev) and Postgres (prod) —
only DATABASE_URL changes.
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

from app.config.settings import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Lazily initialize and return the async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        engine_kwargs: dict = {
            "echo": False,
            "pool_pre_ping": True,
        }
        # SQLite needs special handling for async + WAL mode
        if settings.is_sqlite:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(settings.database_url, **engine_kwargs)
        log.info("database_engine_created", url=_safe_url(settings.database_url))
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager that yields a session and auto-commits on success.

    Usage:
        async with session_scope() as session:
            session.add(obj)
    Rolls back on exception.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Shutdown hook — dispose of the engine connection pool."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        log.info("database_engine_disposed")
    _engine = None
    _session_factory = None


def _safe_url(url: str) -> str:
    """Mask password in URL for logging."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if ":" in rest.split("@", 1)[0]:
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    return url
