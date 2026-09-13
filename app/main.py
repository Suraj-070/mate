"""Application entrypoint.

Wires up logging, runs DB migrations check, starts the Discord bot.
Handles graceful shutdown on Ctrl+C / SIGTERM.
"""
from __future__ import annotations

import asyncio
import signal
import sys

import discord

from app.bot.client import make_bot
from app.config.settings import get_settings
from app.database.connection import dispose_engine
from app.utils.logging import get_logger, setup_logging
from app.workers.scheduler import shutdown_scheduler, start_scheduler


def _check_migrations() -> None:
    """Run Alembic migrations to head before starting the bot.

    We use sync Alembic (via subprocess-style call) to keep migration
    semantics identical to `alembic upgrade head`. If migrations fail,
    we refuse to start the bot.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    log = get_logger(__name__)
    log.info("running_migrations")
    try:
        alembic_cfg = AlembicConfig("alembic.ini")
        # Override URL from env (Alembic reads DATABASE_URL in env.py)
        command.upgrade(alembic_cfg, "head")
        log.info("migrations_complete")
    except Exception as e:
        log.error("migration_failed", error=str(e))
        print(f"❌ Database migration failed: {e}", file=sys.stderr)
        print("   Run `alembic upgrade head` manually to see the full error.", file=sys.stderr)
        sys.exit(1)


async def _ensure_data_dir() -> None:
    """Make sure ./data exists for SQLite if using the default URL."""
    settings = get_settings()
    if settings.is_sqlite:
        from pathlib import Path
        # Extract path from URL: sqlite+aiosqlite:///./data/groupmate.db → ./data/groupmate.db
        url = settings.database_url
        if ":///" in url:
            db_path = url.split(":///", 1)[1]
        else:
            db_path = url.split("://", 1)[1]
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)


async def main_async() -> None:
    """Async entrypoint."""
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger(__name__)

    log.info(
        "startup",
        model=settings.groq_model,
        database="sqlite" if settings.is_sqlite else "postgres",
        buffer_size=settings.context_buffer_size,
        autonomy=settings.enable_autonomous_participation,
        style_learning=settings.enable_style_learning,
        long_term_memory=settings.enable_long_term_memory,
        summaries=settings.enable_conversation_summaries,
        react_outcome=settings.enable_react_outcome,
        llm_assist=settings.enable_llm_decision_assist,
    )

    await _ensure_data_dir()
    _check_migrations()
    await start_scheduler()

    # Phase 2: start the memory expiry sweeper as a background task
    sweeper_task: asyncio.Task | None = None
    if settings.enable_long_term_memory and settings.memory_expiry_sweep_minutes > 0:
        sweeper_task = asyncio.create_task(_run_memory_sweeper())

    bot, tree = make_bot()

    # ── Graceful shutdown ─────────────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler(*_: object) -> None:
        log.info("shutdown_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda *_: None)

    bot_task = asyncio.create_task(bot.start(settings.discord_bot_token))

    done, pending = await asyncio.wait(
        {bot_task, asyncio.create_task(stop_event.wait())},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if sweeper_task is not None and not sweeper_task.done():
        sweeper_task.cancel()
        try:
            await sweeper_task
        except asyncio.CancelledError:
            pass

    if stop_event.is_set():
        log.info("shutting_down_bot")
        await bot.close()
        await bot_task

    await shutdown_scheduler()
    await dispose_engine()

    from app.ai.provider import dispose_provider
    await dispose_provider()

    log.info("shutdown_complete")


async def _run_memory_sweeper() -> None:
    """Background loop that periodically sweeps expired memories.

    Marked as soft-deleted so they stop being returned by retrieval queries.
    Audit trail preserved in DB.
    """
    from app.database.repository import sweep_expired_memories

    settings = get_settings()
    interval_seconds = settings.memory_expiry_sweep_minutes * 60
    log.info("memory_sweeper_started", interval_minutes=settings.memory_expiry_sweep_minutes)

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            count = await sweep_expired_memories()
            if count:
                log.info("memory_sweeper_swept", count=count)
        except asyncio.CancelledError:
            log.info("memory_sweeper_cancelled")
            raise
        except Exception as e:
            log.exception("memory_sweeper_error", error=str(e))
            # Don't die — retry next interval
            await asyncio.sleep(60)


def main() -> None:
    """Synchronous entrypoint for `python -m app.main` or the console script."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        # asyncio.run handles cancellation, but be defensive
        pass


if __name__ == "__main__":
    main()
