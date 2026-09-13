"""Real APScheduler integration (Phase 3).

Replaces the Phase 1 stub. Uses AsyncIOScheduler. On startup, reads all
pending reminders + timers from DB and re-schedules them — restart-safe.

Firing logic:
- Reminder fires → send the reminder message to the channel
                → if recurring, advance trigger_at and re-schedule
                → if one-time, mark as fired
- Timer fires   → send "timer X done" to the channel
                → mark as fired

The scheduler is the ONLY thing that actually fires jobs. The tools create
DB rows + ask the scheduler to register them. On restart, recovery happens
automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.database import repository as repo
from app.tools.time_format import format_display_time, format_duration, next_recurrence_trigger
from app.utils.logging import get_logger

log = get_logger(__name__)


class BotScheduler:
    """Wraps APScheduler with bot-specific firing logic.

    The scheduler needs a reference to the discord.Client so it can fetch
    channels and send messages. Set via `set_client()` after the bot logs in.
    """

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        self._client = None  # discord.Client, set later

    def set_client(self, client) -> None:
        """Set the Discord client. Called from main.py on bot ready."""
        self._client = client

    @property
    def client(self):
        return self._client

    async def start(self) -> None:
        """Initialize and start APScheduler, then recover pending jobs."""
        if self._scheduler is not None:
            log.warning("scheduler_already_started")
            return

        # Use a memory jobstore — we persist job state in our own tables.
        # This means on restart, jobs vanish from APScheduler but our DB
        # recovery re-registers them. Cleaner than fighting APScheduler's
        # SQLAlchemy jobstore with our async setup.
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "misfire_grace_time": 60,
                "coalesce": True,
                "max_instances": 1,
            },
        )
        self._scheduler.start()
        log.info("scheduler_started")

        # Recover pending jobs from DB
        await self._recover_pending_jobs()

    async def shutdown(self) -> None:
        """Stop APScheduler gracefully."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            log.info("scheduler_stopped")
            self._scheduler = None

    async def _recover_pending_jobs(self) -> None:
        """On startup, read all unfired + uncancelled reminders/timers from DB
        and register them with APScheduler.
        """
        if self._scheduler is None:
            return

        reminders = await repo.list_all_pending_reminders_for_recovery()
        timers = await repo.list_all_pending_timers_for_recovery()

        now_utc = datetime.now(timezone.utc)

        for r in reminders:
            try:
                await self._schedule_reminder_internal(
                    reminder_id=r.id,
                    trigger_at=r.trigger_at,
                    misfire_grace=300 if r.trigger_at < now_utc else None,
                )
            except Exception as e:
                log.warning("recovery_schedule_reminder_failed", reminder_id=r.id, error=str(e))

        for t in timers:
            try:
                await self._schedule_timer_internal(
                    timer_id=t.id,
                    expires_at=t.expires_at,
                    misfire_grace=60 if t.expires_at < now_utc else None,
                )
            except Exception as e:
                log.warning("recovery_schedule_timer_failed", timer_id=t.id, error=str(e))

        log.info(
            "recovery_complete",
            reminders_scheduled=len(reminders),
            timers_scheduled=len(timers),
        )

    # ── Reminder scheduling ───────────────────────────────────
    async def schedule_reminder(self, reminder_id: int) -> None:
        """Schedule a single reminder (called by CreateReminderTool)."""
        if self._scheduler is None:
            log.warning("scheduler_not_started_cannot_schedule_reminder")
            return

        reminder = await repo.get_reminder(reminder_id)
        if reminder is None:
            log.warning("schedule_reminder_not_found", reminder_id=reminder_id)
            return

        await self._schedule_reminder_internal(
            reminder_id=reminder_id,
            trigger_at=reminder.trigger_at,
        )

    async def _schedule_reminder_internal(
        self,
        reminder_id: int,
        trigger_at: datetime,
        misfire_grace: Optional[int] = None,
    ) -> None:
        """Internal: register a one-shot DateTrigger job for a reminder."""
        job_id = f"reminder_{reminder_id}"

        # Make trigger tz-aware if not
        if trigger_at.tzinfo is None:
            trigger_at = trigger_at.replace(tzinfo=timezone.utc)

        trigger = DateTrigger(run_date=trigger_at)
        self._scheduler.add_job(
            self._fire_reminder,
            trigger=trigger,
            args=[reminder_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=misfire_grace or 60,
        )
        log.debug("reminder_scheduled", reminder_id=reminder_id, fire_at=trigger_at.isoformat())

    async def unschedule_reminder(self, reminder_id: int) -> None:
        """Remove the APScheduler job for a reminder (called on cancel)."""
        if self._scheduler is None:
            return
        job_id = f"reminder_{reminder_id}"
        try:
            self._scheduler.remove_job(job_id)
            log.debug("reminder_unscheduled", reminder_id=reminder_id)
        except Exception:
            # Job may not exist (already fired, never scheduled, etc.) — that's fine
            pass

    async def _fire_reminder(self, reminder_id: int) -> None:
        """Called by APScheduler when a reminder's trigger fires."""
        reminder = await repo.get_reminder(reminder_id)
        if reminder is None or reminder.is_cancelled:
            log.info("reminder_fire_skipped_cancelled_or_missing", reminder_id=reminder_id)
            return

        if reminder.is_fired:
            log.info("reminder_fire_skipped_already_fired", reminder_id=reminder_id)
            return

        # Send the message
        await self._send_to_channel(
            channel_db_id=reminder.channel_id,
            content=f"⏰ **Reminder:** {reminder.message}",
            ping_user_db_id=reminder.user_id,
        )

        # Update DB
        if reminder.recurrence:
            # Advance to next occurrence + reschedule
            next_trigger = next_recurrence_trigger(reminder.recurrence, datetime.now(timezone.utc))
            await repo.advance_recurring_reminder(reminder_id, next_trigger)
            await self._schedule_reminder_internal(
                reminder_id=reminder_id,
                trigger_at=next_trigger,
            )
            log.info(
                "recurring_reminder_advanced",
                reminder_id=reminder_id,
                next_trigger=next_trigger.isoformat(),
                recurrence=reminder.recurrence,
            )
        else:
            # One-time — mark fired
            await repo.mark_reminder_fired(reminder_id)
            log.info("reminder_fired", reminder_id=reminder_id)

    # ── Timer scheduling ──────────────────────────────────────
    async def schedule_timer(self, timer_id: int) -> None:
        if self._scheduler is None:
            log.warning("scheduler_not_started_cannot_schedule_timer")
            return

        timer = await repo.get_timer(timer_id)
        if timer is None:
            log.warning("schedule_timer_not_found", timer_id=timer_id)
            return

        await self._schedule_timer_internal(
            timer_id=timer_id,
            expires_at=timer.expires_at,
        )

    async def _schedule_timer_internal(
        self,
        timer_id: int,
        expires_at: datetime,
        misfire_grace: Optional[int] = None,
    ) -> None:
        job_id = f"timer_{timer_id}"

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        trigger = DateTrigger(run_date=expires_at)
        self._scheduler.add_job(
            self._fire_timer,
            trigger=trigger,
            args=[timer_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=misfire_grace or 30,
        )
        log.debug("timer_scheduled", timer_id=timer_id, fire_at=expires_at.isoformat())

    async def unschedule_timer(self, timer_id: int) -> None:
        if self._scheduler is None:
            return
        job_id = f"timer_{timer_id}"
        try:
            self._scheduler.remove_job(job_id)
            log.debug("timer_unscheduled", timer_id=timer_id)
        except Exception:
            pass

    async def _fire_timer(self, timer_id: int) -> None:
        timer = await repo.get_timer(timer_id)
        if timer is None or timer.is_cancelled or timer.is_fired:
            log.info("timer_fire_skipped", timer_id=timer_id)
            return

        # Format the message
        duration_str = format_duration(timer.duration_seconds)
        label_note = f" ({timer.label})" if timer.label else ""
        content = f"⏱️ **Timer done{label_note}** — {duration_str} elapsed"

        await self._send_to_channel(
            channel_db_id=timer.channel_id,
            content=content,
            ping_user_db_id=timer.user_id,
        )
        await repo.mark_timer_fired(timer_id)
        log.info("timer_fired", timer_id=timer_id)

    # ── Discord send helper ───────────────────────────────────
    async def _send_to_channel(
        self,
        channel_db_id: int,
        content: str,
        ping_user_db_id: Optional[int] = None,
    ) -> None:
        """Send a message to a Discord channel identified by its DB ID.

        Looks up the Discord channel ID from DB, fetches the channel via
        the bot client, and sends the message. Optionally pings the user
        who created the reminder/timer.
        """
        if self._client is None:
            log.error("scheduler_send_no_client", channel_db_id=channel_db_id)
            return

        # Look up the Discord channel ID
        from sqlalchemy import select
        from app.database.connection import session_scope
        from app.database.models import Channel
        async with session_scope() as session:
            result = await session.execute(
                select(Channel).where(Channel.id == channel_db_id)
            )
            channel_row = result.scalar_one_or_none()
            if channel_row is None:
                log.error("scheduler_send_channel_not_in_db", channel_db_id=channel_db_id)
                return
            discord_channel_id = int(channel_row.discord_channel_id)

        # Fetch the channel via the bot client
        channel = self._client.get_channel(discord_channel_id)
        if channel is None:
            # Try to fetch it (might not be in cache)
            try:
                channel = await self._client.fetch_channel(discord_channel_id)
            except Exception as e:
                log.error("scheduler_send_fetch_channel_failed", channel_id=discord_channel_id, error=str(e))
                return

        # Optional: prepend a user mention
        final_content = content
        if ping_user_db_id is not None:
            from sqlalchemy import select as sa_select
            from app.database.models import User
            async with session_scope() as session:
                result = await session.execute(
                    sa_select(User).where(User.id == ping_user_db_id)
                )
                user_row = result.scalar_one_or_none()
                if user_row is not None:
                    final_content = f"<@{user_row.discord_user_id}> {content}"

        try:
            await channel.send(final_content)
        except Exception as e:
            log.error("scheduler_send_failed", channel_id=discord_channel_id, error=str(e))


# ── Singleton ─────────────────────────────────────────────────
_scheduler: BotScheduler | None = None


def get_scheduler() -> BotScheduler | None:
    """Returns the singleton BotScheduler, or None if not started yet."""
    return _scheduler


def set_scheduler(scheduler: BotScheduler) -> None:
    """Inject the scheduler instance — used by main.py."""
    global _scheduler
    _scheduler = scheduler


# ── Compatibility shims for main.py startup/shutdown hooks ────
async def start_scheduler() -> None:
    """Called by main.py on startup. Creates + starts the BotScheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BotScheduler()
    await _scheduler.start()


async def shutdown_scheduler() -> None:
    """Called by main.py on shutdown."""
    global _scheduler
    if _scheduler is not None:
        await _scheduler.shutdown()
        # Keep the instance around so it can be restarted if needed
