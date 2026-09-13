"""Shared time-formatting + recurrence helpers used by both tools and the scheduler.

Lives here (not in tools/reminders/service.py or tools/timers/service.py) to
avoid circular imports:
  - tools/reminders/service.py imports from workers/scheduler.py (to call get_scheduler)
  - workers/scheduler.py needs next_recurrence_trigger to advance recurring reminders
  - Both can safely import from this module with no cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytz

from app.config.settings import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)


def format_display_time(utc_dt: datetime) -> str:
    """Format a UTC datetime as a friendly local-time string for display."""
    settings_tz = get_settings().user_timezone
    try:
        tz = pytz.timezone(settings_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    local = utc_dt.astimezone(tz)
    return local.strftime("%a %b %d, %I:%M %p").strip()


def format_duration(seconds: int) -> str:
    """Format a duration (in seconds) as a human-readable string."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        mins = seconds // 60
        return f"{mins} minute{'s' if mins != 1 else ''}"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    if mins == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {mins}m"


def next_recurrence_trigger(recurrence: str, just_fired_at: datetime) -> datetime:
    """Given a recurrence pattern, compute the next trigger after `just_fired_at`.

    `just_fired_at` should be UTC. Returns UTC datetime.
    """
    settings_tz = get_settings().user_timezone
    try:
        tz = pytz.timezone(settings_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    local_just_fired = just_fired_at.astimezone(tz)

    if recurrence == "daily":
        next_local = local_just_fired + timedelta(days=1)
    elif recurrence == "weekly":
        next_local = local_just_fired + timedelta(weeks=1)
    elif recurrence == "weekdays":
        # Skip Saturday(5) and Sunday(6)
        next_local = local_just_fired + timedelta(days=1)
        while next_local.weekday() >= 5:
            next_local = next_local + timedelta(days=1)
    elif recurrence == "weekends":
        # Skip Mon-Fri (0-4)
        next_local = local_just_fired + timedelta(days=1)
        while next_local.weekday() < 5:
            next_local = next_local + timedelta(days=1)
    else:
        # Unknown recurrence — fall back to daily
        log.warning("unknown_recurrence_pattern_defaulting_to_daily", recurrence=recurrence)
        next_local = local_just_fired + timedelta(days=1)

    return next_local.astimezone(timezone.utc)
