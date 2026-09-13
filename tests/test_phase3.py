"""Tests for Phase 3: tool framework, reminders, timers, scheduler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.tools.reminders.models import CreateReminderArgs
from app.tools.reminders.service import parse_natural_time
from app.tools.schemas import ToolContext, ToolResult
from app.tools.timers.service import parse_duration
from app.tools.time_format import (
    format_display_time,
    format_duration,
    next_recurrence_trigger,
)


# ── Duration parsing ──────────────────────────────────────────
class TestParseDuration:
    def test_minutes(self):
        sec, err = parse_duration("10 minutes")
        assert err is None
        assert sec == 600

    def test_short_minutes(self):
        sec, err = parse_duration("5m")
        assert err is None
        assert sec == 300

    def test_hours(self):
        sec, err = parse_duration("1 hour")
        assert err is None
        assert sec == 3600

    def test_short_hours(self):
        sec, err = parse_duration("2h")
        assert err is None
        assert sec == 7200

    def test_seconds(self):
        sec, err = parse_duration("90 seconds")
        assert err is None
        assert sec == 90

    def test_combined(self):
        sec, err = parse_duration("2h30m")
        assert err is None
        assert sec == 9000

    def test_empty(self):
        sec, err = parse_duration("")
        assert err is not None
        assert sec is None

    def test_unparseable(self):
        sec, err = parse_duration("five moons from now")
        assert err is not None
        assert sec is None

    def test_too_long(self):
        sec, err = parse_duration("48 hours")
        assert err is not None
        assert "24 hours" in err


# ── Natural-language time parsing ─────────────────────────────
class TestParseNaturalTime:
    def test_in_2_hours(self):
        utc, err = parse_natural_time("in 2 hours")
        assert err is None
        assert utc is not None
        # Should be roughly 2 hours from now (within a 10-minute window)
        delta = utc - datetime.now(timezone.utc)
        assert 110 * 60 <= delta.total_seconds() <= 130 * 60

    def test_in_30_minutes(self):
        utc, err = parse_natural_time("in 30 minutes")
        assert err is None
        assert utc is not None
        delta = utc - datetime.now(timezone.utc)
        assert 25 * 60 <= delta.total_seconds() <= 35 * 60

    def test_empty(self):
        utc, err = parse_natural_time("")
        assert err is not None
        assert utc is None

    def test_past_time(self):
        utc, err = parse_natural_time("yesterday at 8am")
        assert err is not None
        assert "past" in err

    def test_unparseable(self):
        utc, err = parse_natural_time("when the cows come home")
        # dateparser may or may not parse this — just make sure we don't crash
        # and the result is consistent
        assert (utc is None) == (err is not None)


# ── Recurrence logic ─────────────────────────────────────────
class TestNextRecurrenceTrigger:
    def test_daily(self):
        now = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        nxt = next_recurrence_trigger("daily", now)
        delta = nxt - now
        assert 23 * 3600 <= delta.total_seconds() <= 25 * 3600

    def test_weekly(self):
        now = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        nxt = next_recurrence_trigger("weekly", now)
        delta = nxt - now
        assert 6 * 24 * 3600 <= delta.total_seconds() <= 8 * 24 * 3600

    def test_unknown_recurrence_falls_back_to_daily(self):
        now = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        nxt = next_recurrence_trigger("bogus", now)
        delta = nxt - now
        assert 23 * 3600 <= delta.total_seconds() <= 25 * 3600


# ── Format helpers ───────────────────────────────────────────
class TestFormatHelpers:
    def test_format_duration_short(self):
        assert "30 second" in format_duration(30)

    def test_format_duration_minutes(self):
        assert "10 minute" in format_duration(600)

    def test_format_duration_hours(self):
        assert "1 hour" in format_duration(3600)

    def test_format_duration_combined(self):
        result = format_duration(9000)  # 2h30m
        assert "2h" in result
        assert "30m" in result

    def test_format_display_time_returns_string(self):
        utc = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        result = format_display_time(utc)
        assert isinstance(result, str)
        assert len(result) > 0


# ── ToolResult serialization ─────────────────────────────────
class TestToolResult:
    def test_to_llm_string_success(self):
        result = ToolResult(
            success=True,
            data={"reminder_id": 42},
            user_message="reminder set",
        )
        s = result.to_llm_string()
        assert '"success": true' in s
        assert '"reminder_id": 42' in s
        assert '"suggested_message": "reminder set"' in s

    def test_to_llm_string_failure(self):
        result = ToolResult(
            success=False,
            error_message="invalid time",
            user_message="couldn't parse that",
        )
        s = result.to_llm_string()
        assert '"success": false' in s
        assert '"error": "invalid time"' in s


# ── Tool registry + executor ──────────────────────────────────
class TestToolRegistry:
    def test_registry_auto_loads_tools(self):
        from app.tools.registry import get_default_registry, reset_registry
        reset_registry()
        registry = get_default_registry()
        assert len(registry) >= 6  # 3 reminder + 3 timer tools
        assert "create_reminder" in registry
        assert "list_reminders" in registry
        assert "cancel_reminder" in registry
        assert "start_timer" in registry
        assert "list_timers" in registry
        assert "cancel_timer" in registry

    def test_all_schemas_valid(self):
        from app.tools.registry import get_default_registry, reset_registry
        reset_registry()
        registry = get_default_registry()
        schemas = registry.all_schemas()
        assert len(schemas) >= 6
        for s in schemas:
            assert s["type"] == "function"
            assert "name" in s["function"]
            assert "description" in s["function"]
            assert "parameters" in s["function"]


class TestExecuteToolCall:
    """Tests for the executor — uses the real DB to verify end-to-end."""

    def test_unknown_tool_returns_error(self):
        async def _run():
            from app.tools.executor import execute_tool_call
            ctx = ToolContext(
                user_db_id=1, discord_user_id="123",
                user_display_name="Alice", is_admin=False,
                channel_db_id=1, discord_channel_id="999",
            )
            result = await execute_tool_call("nonexistent_tool", {}, ctx)
            assert result.success is False
            assert "unknown tool" in result.error_message
        asyncio.run(_run())

    def test_invalid_args_returns_error(self):
        async def _run():
            from app.tools.executor import execute_tool_call
            ctx = ToolContext(
                user_db_id=1, discord_user_id="123",
                user_display_name="Alice", is_admin=False,
                channel_db_id=1, discord_channel_id="999",
            )
            # Missing required 'when' field
            result = await execute_tool_call("create_reminder", {"message": "test"}, ctx)
            assert result.success is False
            assert "invalid arguments" in result.error_message
        asyncio.run(_run())

    def test_create_reminder_end_to_end(self):
        """Full happy-path: create a reminder via the executor."""
        async def _run():
            from app.database import repository as repo
            from app.tools.executor import execute_tool_call

            # Setup: channel + user
            chan = await repo.get_or_create_channel("g1", "ch-1", "test-channel")
            user = await repo.get_or_create_user(
                "u1", username="alice", display_name="Alice"
            )

            ctx = ToolContext(
                user_db_id=user.id, discord_user_id="u1",
                user_display_name="Alice", is_admin=False,
                channel_db_id=chan.id, discord_channel_id="ch-1",
            )

            result = await execute_tool_call(
                "create_reminder",
                {"when": "in 2 hours", "message": "call home"},
                ctx,
            )
            assert result.success is True
            assert result.data["reminder_id"] > 0
            assert "call home" in result.user_message

            # Verify it's persisted
            reminders = await repo.list_pending_reminders(user_db_id=user.id)
            assert len(reminders) >= 1
            found = [r for r in reminders if r.id == result.data["reminder_id"]]
            assert len(found) == 1
            assert found[0].message == "call home"
        asyncio.run(_run())

    def test_create_timer_end_to_end(self):
        async def _run():
            from app.database import repository as repo
            from app.tools.executor import execute_tool_call

            chan = await repo.get_or_create_channel("g1", "ch-t", "test-channel-t")
            user = await repo.get_or_create_user(
                "u2", username="bob", display_name="Bob"
            )

            ctx = ToolContext(
                user_db_id=user.id, discord_user_id="u2",
                user_display_name="Bob", is_admin=False,
                channel_db_id=chan.id, discord_channel_id="ch-t",
            )

            result = await execute_tool_call(
                "start_timer",
                {"duration": "5 minutes", "label": "tea steeping"},
                ctx,
            )
            assert result.success is True
            assert result.data["timer_id"] > 0
            assert result.data["duration_seconds"] == 300
            assert "5 minute" in result.user_message

            timers = await repo.list_pending_timers(user_db_id=user.id)
            assert len(timers) >= 1
        asyncio.run(_run())

    def test_cancel_other_users_reminder_denied(self):
        """User A creates reminder; user B tries to cancel — denied."""
        async def _run():
            from app.database import repository as repo
            from app.tools.executor import execute_tool_call

            chan = await repo.get_or_create_channel("g1", "ch-2", "test-channel-2")
            alice = await repo.get_or_create_user(
                "u3", username="alice2", display_name="Alice2"
            )
            bob = await repo.get_or_create_user(
                "u4", username="bob2", display_name="Bob2", is_admin=False
            )

            # Alice creates reminder
            alice_ctx = ToolContext(
                user_db_id=alice.id, discord_user_id="u3",
                user_display_name="Alice2", is_admin=False,
                channel_db_id=chan.id, discord_channel_id="ch-2",
            )
            create_result = await execute_tool_call(
                "create_reminder",
                {"when": "in 2 hours", "message": "alice's reminder"},
                alice_ctx,
            )
            assert create_result.success

            # Bob tries to cancel
            bob_ctx = ToolContext(
                user_db_id=bob.id, discord_user_id="u4",
                user_display_name="Bob2", is_admin=False,
                channel_db_id=chan.id, discord_channel_id="ch-2",
            )
            cancel_result = await execute_tool_call(
                "cancel_reminder",
                {"reminder_id": create_result.data["reminder_id"]},
                bob_ctx,
            )
            assert cancel_result.success is False
            assert "your own" in cancel_result.user_message
        asyncio.run(_run())

    def test_admin_can_cancel_other_users_reminder(self):
        async def _run():
            from app.database import repository as repo
            from app.tools.executor import execute_tool_call

            chan = await repo.get_or_create_channel("g1", "ch-3", "test-channel-3")
            alice = await repo.get_or_create_user(
                "u5", username="alice3", display_name="Alice3"
            )
            admin = await repo.get_or_create_user(
                "u6", username="admin", display_name="Admin", is_admin=True
            )

            # Alice creates reminder
            alice_ctx = ToolContext(
                user_db_id=alice.id, discord_user_id="u5",
                user_display_name="Alice3", is_admin=False,
                channel_db_id=chan.id, discord_channel_id="ch-3",
            )
            create_result = await execute_tool_call(
                "create_reminder",
                {"when": "in 2 hours", "message": "alice's reminder"},
                alice_ctx,
            )
            assert create_result.success

            # Admin cancels it
            admin_ctx = ToolContext(
                user_db_id=admin.id, discord_user_id="u6",
                user_display_name="Admin", is_admin=True,
                channel_db_id=chan.id, discord_channel_id="ch-3",
            )
            cancel_result = await execute_tool_call(
                "cancel_reminder",
                {"reminder_id": create_result.data["reminder_id"]},
                admin_ctx,
            )
            assert cancel_result.success is True
        asyncio.run(_run())


# ── Tool arg validation ──────────────────────────────────────
class TestToolArgValidation:
    def test_create_reminder_args_valid(self):
        args = CreateReminderArgs(when="in 1 hour", message="test")
        assert args.when == "in 1 hour"
        assert args.message == "test"
        assert args.recurrence is None

    def test_create_reminder_args_recurring(self):
        args = CreateReminderArgs(
            when="tomorrow at 8am",
            message="standup",
            recurrence="daily",
        )
        assert args.recurrence == "daily"

    def test_create_reminder_args_invalid_recurrence(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreateReminderArgs(
                when="tomorrow",
                message="test",
                recurrence="bogus",
            )

    def test_create_reminder_args_empty_message_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreateReminderArgs(when="in 1 hour", message="")
