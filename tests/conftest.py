"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sure the project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set required env vars BEFORE importing any app module that calls get_settings().
# Tests use placeholder values; the actual values are mocked where needed.
# IMPORTANT: override any system-wide DATABASE_URL that might leak in.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test.db"
os.environ.setdefault("DISCORD_BOT_TOKEN", "test_token_123456789")
os.environ.setdefault("DISCORD_APP_ID", "123456789012345678")
os.environ.setdefault("ZAI_API_KEY", "test_zai_key_abc")
os.environ.setdefault("DISCORD_ADMIN_IDS", "111111111111111111")


import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Create all tables in the test SQLite DB at session start.

    Drops + recreates to ensure a clean slate. Runs once per test session.
    """
    import asyncio
    from app.database.connection import get_engine, dispose_engine
    from app.database.models import Base

    async def _create():
        # Make sure data dir exists
        from pathlib import Path
        Path("./data").mkdir(exist_ok=True)
        # Clean up old test DB
        db_path = Path("./data/test.db")
        if db_path.exists():
            db_path.unlink()

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await dispose_engine()

    asyncio.run(_create())
    yield
    # Cleanup at end of session
    asyncio.run(dispose_engine())


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset cached singletons between tests so each test starts fresh."""
    # Clear settings cache
    from app.config import settings as settings_mod
    settings_mod.get_settings.cache_clear()

    # Clear provider singleton
    from app.ai import provider as provider_mod
    provider_mod._provider = None

    # Clear buffer singleton
    from app.context import conversation_buffer as buffer_mod
    buffer_mod._buffer = None

    # Clear style profile cache
    from app.context import builder as builder_mod
    builder_mod._style_profile_cache.clear()

    # Phase 3: Clear tool registry so it gets re-initialized per test
    from app.tools import registry as registry_mod
    registry_mod.reset_registry()

    yield
