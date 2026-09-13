"""Reminders service — natural-language time parsing + scheduling.

The tool implementations (CreateReminderTool, etc.) live here. Each tool:
  1. Validates args (already done by the framework via args_model)
  2. Checks permissions
  3. Calls the scheduler + repository to actually create/list/cancel
  4. Returns a ToolResult

Time parsing uses `dateparser` which handles phrases like:
  "tomorrow at 8pm"
  "in 2 hours"
  "monday 9am"
  "2025-03-15 14:30"

We parse in the bot's configured timezone (Australia/Sydney by default) and
convert to UTC for storage. The scheduler fires at the stored UTC time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import dateparser
import pytz

from app.config.settings import get_settings
from app.database import repository as repo
from app.tools.registry import BaseTool
from app.tools.reminders.models import (
    CancelReminderArgs,
    CreateReminderArgs,
    ListRemindersArgs,
)
from app.tools.schemas import ToolContext, ToolResult
from app.tools.time_format import format_display_time, next_recurrence_trigger
from app.utils.logging import get_logger
from app.workers.scheduler import get_scheduler

log = get_logger(__name__)


# ── Time parsing ──────────────────────────────────────────────
def parse_natural_time(when_str: str) -> tuple[Optional[datetime], Optional[str]]:
    """Parse a natural-language time string.

    Returns (utc_datetime, None) on success, (None, error_message) on failure.
    """
    if not when_str or not when_str.strip():
        return None, "empty time string"

    settings_tz = get_settings().user_timezone
    try:
        tz = pytz.timezone(settings_tz)
    except pytz.UnknownTimeZoneError:
        log.warning("unknown_timezone_falling_back_to_utc", tz=settings_tz)
        tz = pytz.UTC

    # Parse with dateparser, anchored to the configured timezone
    parsed = dateparser.parse(
        when_str,
        settings={
            "TIMEZONE": settings_tz,
            "TO_TIMEZONE": "UTC",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "current_period",
            "RELATIVE_BASE": datetime.now(tz),
        },
    )

    if parsed is None:
        return None, f"couldn't parse '{when_str}' as a time"

    # Convert to UTC for storage
    if parsed.tzinfo is None:
        # dateparser should have set it, but be defensive
        parsed = tz.localize(parsed).astimezone(timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    # Sanity check: don't allow reminders in the past
    now_utc = datetime.now(timezone.utc)
    if parsed < now_utc:
        return None, f"that time is in the past ({parsed.isoformat()})"

    return parsed, None


# ── Tool: CreateReminder ─────────────────────────────────────
class CreateReminderTool(BaseTool):
    name = "create_reminder"
    description = (
        "Create a reminder that fires at a specific time. Use this when the user "
        "asks to be reminded about something. Supports natural-language times "
        "like 'tomorrow at 8pm', 'in 2 hours', 'monday 9am'. Supports optional "
        "recurrence (daily/weekly/weekdays/weekends)."
    )
    args_model = CreateReminderArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": "When to fire the reminder. Natural language OK.",
            },
            "message": {
                "type": "string",
                "description": "What to remind about. Will be sent verbatim.",
            },
            "recurrence": {
                "type": "string",
                "enum": ["daily", "weekly", "weekdays", "weekends"],
                "description": "Optional. Omit for one-time reminder.",
            },
        },
        "required": ["when", "message"],
    }

    async def execute(self, args: CreateReminderArgs, ctx: ToolContext) -> ToolResult:
        trigger_at, parse_err = parse_natural_time(args.when)
        if parse_err:
            return ToolResult(
                success=False,
                error_message=parse_err,
                user_message=f"I couldn't parse '{args.when}' as a time. Try something like 'tomorrow at 8pm' or 'in 2 hours'.",
            )

        # Persist
        reminder = await repo.create_reminder(
            user_db_id=ctx.user_db_id,
            channel_db_id=ctx.channel_db_id,
            trigger_at=trigger_at,
            message=args.message,
            recurrence=args.recurrence,
        )

        # Schedule with APScheduler
        scheduler = get_scheduler()
        if scheduler is not None:
            await scheduler.schedule_reminder(reminder.id)
        else:
            log.warning("scheduler_not_available_reminder_only_persisted", reminder_id=reminder.id)

        display_time = format_display_time(trigger_at)
        recurrence_note = f" (recurring {args.recurrence})" if args.recurrence else ""

        return ToolResult(
            success=True,
            data={
                "reminder_id": reminder.id,
                "trigger_at_utc": trigger_at.isoformat(),
                "trigger_at_display": display_time,
                "recurrence": args.recurrence,
            },
            user_message=f"got it — reminding you {display_time}{recurrence_note}: {args.message} (id #{reminder.id})",
        )


# ── Tool: ListReminders ──────────────────────────────────────
class ListRemindersTool(BaseTool):
    name = "list_reminders"
    description = (
        "List pending reminders. By default lists reminders created by the "
        "current user. Use scope='channel' to list all reminders in this channel."
    )
    args_model = ListRemindersArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["mine", "channel"],
                "description": "Which reminders to list. Default: mine.",
            },
        },
    }

    def check_permissions(self, ctx: ToolContext, args: ListRemindersArgs) -> Optional[str]:
        if args.scope == "channel" and not ctx.is_admin:
            return "only admins can list all reminders in a channel"
        return None

    async def execute(self, args: ListRemindersArgs, ctx: ToolContext) -> ToolResult:
        if args.scope == "mine":
            reminders = await repo.list_pending_reminders(
                user_db_id=ctx.user_db_id,
                limit=20,
            )
            scope_label = "your reminders"
        else:
            reminders = await repo.list_pending_reminders(
                channel_db_id=ctx.channel_db_id,
                limit=20,
            )
            scope_label = "reminders in this channel"

        if not reminders:
            return ToolResult(
                success=True,
                data={"count": 0, "reminders": []},
                user_message=f"no {scope_label} yet",
            )

        formatted = []
        for r in reminders:
            formatted.append(
                {
                    "id": r.id,
                    "trigger_at_display": format_display_time(r.trigger_at),
                    "message": r.message,
                    "recurrence": r.recurrence,
                }
            )

        # Build a compact user-facing summary
        lines = [f"**{scope_label}:**"]
        for r in reminders[:10]:
            tag = f" [{r.recurrence}]" if r.recurrence else ""
            lines.append(f"`#{r.id}` {format_display_time(r.trigger_at)}{tag} — {r.message}")
        if len(reminders) > 10:
            lines.append(f"... and {len(reminders) - 10} more")

        return ToolResult(
            success=True,
            data={"count": len(reminders), "reminders": formatted},
            user_message="\n".join(lines),
        )


# ── Tool: CancelReminder ─────────────────────────────────────
class CancelReminderTool(BaseTool):
    name = "cancel_reminder"
    description = (
        "Cancel a pending reminder by ID. Only the creator or an admin can cancel."
    )
    args_model = CancelReminderArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "reminder_id": {
                "type": "integer",
                "description": "The reminder ID to cancel.",
            },
        },
        "required": ["reminder_id"],
    }

    def check_permissions(self, ctx: ToolContext, args: CancelReminderArgs) -> Optional[str]:
        # Permission check happens in execute() where we have the reminder row
        # to compare against the creator. We allow execution here and let
        # execute() enforce ownership.
        return None

    async def execute(self, args: CancelReminderArgs, ctx: ToolContext) -> ToolResult:
        reminder = await repo.get_reminder(args.reminder_id)
        if reminder is None or reminder.is_fired or reminder.is_cancelled:
            return ToolResult(
                success=False,
                error_message="reminder not found or already fired/cancelled",
                user_message=f"no active reminder with id #{args.reminder_id}",
            )

        # Permission: creator OR admin
        if reminder.user_id != ctx.user_db_id and not ctx.is_admin:
            return ToolResult(
                success=False,
                error_message="user is not the creator and not admin",
                user_message="you can only cancel your own reminders",
            )

        cancelled = await repo.cancel_reminder(args.reminder_id)
        if not cancelled:
            return ToolResult(
                success=False,
                error_message="cancel returned 0 rows updated",
                user_message="couldn't cancel — it may have already fired",
            )

        # Unschedule from APScheduler
        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler.unschedule_reminder(args.reminder_id)

        return ToolResult(
            success=True,
            data={"reminder_id": args.reminder_id},
            user_message=f"cancelled reminder #{args.reminder_id}",
        )
