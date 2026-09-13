"""Timers service — short-duration timer creation + scheduling.

Timers differ from reminders in intent:
- Timers are SHORT (seconds/minutes/hours, not days)
- Timers fire in the channel where they were created
- Timers are one-shot (no recurrence)
- Timers say "timer X finished" rather than a freeform message

We reuse the scheduler infrastructure for simplicity. Both timers and reminders
end up as scheduled jobs in APScheduler.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import repository as repo
from app.tools.registry import BaseTool
from app.tools.schemas import ToolContext, ToolResult
from app.tools.time_format import format_duration
from app.tools.timers.models import CancelTimerArgs, ListTimersArgs, StartTimerArgs
from app.utils.logging import get_logger
from app.workers.scheduler import get_scheduler

log = get_logger(__name__)


# ── Duration parsing ──────────────────────────────────────────
# Pattern matches things like "10 minutes", "5m", "1 hour", "2h30m", "90 seconds"
# Each pattern matches a (number, unit) pair. We scan text left-to-right
# accumulating all matches — this lets "2h30m" parse as 2h + 30m.
_DURATION_PATTERNS = [
    # hours: "1 hour", "2 hours", "1hr", "2hrs", "1h"
    (re.compile(r"(\d+)\s*(?:hours?|hrs?|h)", re.IGNORECASE), "hours"),
    # minutes: "10 minutes", "5 mins", "5m" — note we need to be careful
    # that "m" alone doesn't match the "m" in "minutes" again. We use a
    # longer-first ordering: try "minutes/mins/m" together.
    (re.compile(r"(\d+)\s*(?:minutes?|mins?|m)", re.IGNORECASE), "minutes"),
    # seconds: "30 seconds", "30 secs", "30s"
    (re.compile(r"(\d+)\s*(?:seconds?|secs?|s)", re.IGNORECASE), "seconds"),
]


def _strip_consumed_units(text: str) -> str:
    """Remove the unit suffixes from text after a unit match, to prevent
    the "5m" pattern from also matching the "m" inside "5minutes".

    We replace any of the unit keywords with a single space, then re-scan.
    """
    # Replace full words first (longer matches first to avoid partial overlap)
    text = re.sub(r"\b(?:hours?|hrs?|h)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:minutes?|mins?|m)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:seconds?|secs?|s)\b", " ", text, flags=re.IGNORECASE)
    return text


def parse_duration(duration_str: str) -> tuple[Optional[int], Optional[str]]:
    """Parse a natural-language duration string into seconds.

    Returns (seconds, None) on success, (None, error_message) on failure.
    Caps total duration at 24 hours (86400 sec) — use reminders for longer.

    Handles compound expressions like "2h30m" by matching each unit separately.
    After matching, we strip the matched unit tokens to prevent double-counting.
    """
    if not duration_str or not duration_str.strip():
        return None, "empty duration"

    text = duration_str.strip()
    total_seconds = 0
    matched_any = False
    consumed_spans: list[tuple[int, int]] = []

    for pattern, unit in _DURATION_PATTERNS:
        for match in pattern.finditer(text):
            # Check if this match's span overlaps with one we've already consumed
            span = match.span()
            if any(s[0] < span[1] and span[0] < s[1] for s in consumed_spans):
                continue  # overlapping — skip
            value = int(match.group(1))
            if unit == "hours":
                total_seconds += value * 3600
            elif unit == "minutes":
                total_seconds += value * 60
            else:
                total_seconds += value
            matched_any = True
            consumed_spans.append(span)

    if not matched_any:
        # Try bare-number shorthands: "5m", "2h", "30s"
        m = re.match(r"^(\d+)\s*m$", text)
        if m:
            total_seconds = int(m.group(1)) * 60
            matched_any = True
        else:
            m = re.match(r"^(\d+)\s*h$", text)
            if m:
                total_seconds = int(m.group(1)) * 3600
                matched_any = True
            else:
                m = re.match(r"^(\d+)\s*s$", text)
                if m:
                    total_seconds = int(m.group(1))
                    matched_any = True

    if not matched_any:
        return None, f"couldn't parse '{duration_str}' as a duration"

    if total_seconds <= 0:
        return None, "duration must be positive"

    if total_seconds > 86400:
        return None, "timers can't be longer than 24 hours — use a reminder instead"

    return total_seconds, None


# ── Tool: StartTimer ──────────────────────────────────────────
class StartTimerTool(BaseTool):
    name = "start_timer"
    description = (
        "Start a short-duration timer (minutes/hours, max 24h) that fires in "
        "this channel when done. Use for things like 'set a timer for 10 minutes'. "
        "For longer or scheduled reminders, use create_reminder instead."
    )
    args_model = StartTimerArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "duration": {
                "type": "string",
                "description": "How long. Natural language OK. e.g. '10 minutes', '5m', '1 hour', '2h30m'.",
            },
            "label": {
                "type": "string",
                "description": "Optional label shown when the timer fires.",
            },
        },
        "required": ["duration"],
    }

    async def execute(self, args: StartTimerArgs, ctx: ToolContext) -> ToolResult:
        duration_sec, parse_err = parse_duration(args.duration)
        if parse_err:
            return ToolResult(
                success=False,
                error_message=parse_err,
                user_message=f"I couldn't parse '{args.duration}' as a duration. Try '10 minutes' or '1 hour'.",
            )

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)

        timer = await repo.create_timer(
            user_db_id=ctx.user_db_id,
            channel_db_id=ctx.channel_db_id,
            duration_seconds=duration_sec,
            expires_at=expires_at,
            label=args.label,
        )

        # Schedule
        scheduler = get_scheduler()
        if scheduler is not None:
            await scheduler.schedule_timer(timer.id)
        else:
            log.warning("scheduler_not_available_timer_only_persisted", timer_id=timer.id)

        display = format_duration(duration_sec)
        label_note = f" ({args.label})" if args.label else ""

        return ToolResult(
            success=True,
            data={
                "timer_id": timer.id,
                "duration_seconds": duration_sec,
                "expires_at_utc": expires_at.isoformat(),
                "duration_display": display,
            },
            user_message=f"timer set for {display}{label_note} (id #{timer.id}) — I'll ping you when it's done",
        )


# ── Tool: ListTimers ─────────────────────────────────────────
class ListTimersTool(BaseTool):
    name = "list_timers"
    description = "List active timers. Default: your timers. Use scope='channel' for all in this channel."
    args_model = ListTimersArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["mine", "channel"],
                "description": "Which timers to list. Default: mine.",
            },
        },
    }

    def check_permissions(self, ctx: ToolContext, args: ListTimersArgs) -> Optional[str]:
        if args.scope == "channel" and not ctx.is_admin:
            return "only admins can list all timers in a channel"
        return None

    async def execute(self, args: ListTimersArgs, ctx: ToolContext) -> ToolResult:
        if args.scope == "mine":
            timers = await repo.list_pending_timers(user_db_id=ctx.user_db_id, limit=20)
            scope_label = "your timers"
        else:
            timers = await repo.list_pending_timers(channel_db_id=ctx.channel_db_id, limit=20)
            scope_label = "timers in this channel"

        if not timers:
            return ToolResult(
                success=True,
                data={"count": 0, "timers": []},
                user_message=f"no {scope_label} active",
            )

        now_utc = datetime.now(timezone.utc)
        lines = [f"**{scope_label}:**"]
        timer_data = []
        for t in timers[:10]:
            # SQLite may return tz-naive datetimes — coerce to UTC
            expires_at = t.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            remaining = (expires_at - now_utc).total_seconds()
            timer_data.append({"id": t.id, "remaining_sec": int(remaining)})
            if remaining < 0:
                remaining_str = "(overdue)"
            else:
                remaining_str = f"{int(remaining // 60)}m{int(remaining % 60)}s left"
            label_note = f" [{t.label}]" if t.label else ""
            lines.append(f"`#{t.id}` {format_duration(t.duration_seconds)} — {remaining_str}{label_note}")
        if len(timers) > 10:
            lines.append(f"... and {len(timers) - 10} more")

        return ToolResult(
            success=True,
            data={"count": len(timers), "timers": timer_data},
            user_message="\n".join(lines),
        )


# ── Tool: CancelTimer ───────────────────────────────────────
class CancelTimerTool(BaseTool):
    name = "cancel_timer"
    description = "Cancel an active timer by ID. Only the creator or an admin can cancel."
    args_model = CancelTimerArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "timer_id": {
                "type": "integer",
                "description": "The timer ID to cancel.",
            },
        },
        "required": ["timer_id"],
    }

    def check_permissions(self, ctx: ToolContext, args: CancelTimerArgs) -> Optional[str]:
        return None  # checked in execute via ownership lookup

    async def execute(self, args: CancelTimerArgs, ctx: ToolContext) -> ToolResult:
        timer = await repo.get_timer(args.timer_id)
        if timer is None or timer.is_fired or timer.is_cancelled:
            return ToolResult(
                success=False,
                error_message="timer not found or already fired/cancelled",
                user_message=f"no active timer with id #{args.timer_id}",
            )

        if timer.user_id != ctx.user_db_id and not ctx.is_admin:
            return ToolResult(
                success=False,
                error_message="user is not the creator and not admin",
                user_message="you can only cancel your own timers",
            )

        cancelled = await repo.cancel_timer(args.timer_id)
        if not cancelled:
            return ToolResult(
                success=False,
                error_message="cancel returned 0 rows updated",
                user_message="couldn't cancel — it may have already fired",
            )

        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler.unschedule_timer(args.timer_id)

        return ToolResult(
            success=True,
            data={"timer_id": args.timer_id},
            user_message=f"cancelled timer #{args.timer_id}",
        )
