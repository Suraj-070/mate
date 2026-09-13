"""Pydantic schemas for the reminders tool."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class CreateReminderArgs(BaseModel):
    """Args for creating a reminder.

    The LLM passes `when` as a natural-language string. We parse it server-side
    with dateparser using the configured timezone.

    `recurrence` is optional. If provided, must be one of the supported patterns.
    """

    when: str = Field(
        ...,
        description="When to fire the reminder, in natural language. "
                    "Examples: 'tomorrow at 8pm', 'in 2 hours', 'monday 9am', "
                    "'2025-03-15 14:30'. Interpreted in the bot's timezone.",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="What to remind about. Short, direct. Will be sent verbatim.",
    )
    recurrence: Optional[Literal["daily", "weekly", "weekdays", "weekends"]] = Field(
        default=None,
        description="Optional recurrence pattern. Omit for a one-time reminder.",
    )


class ListRemindersArgs(BaseModel):
    """Args for listing reminders."""

    scope: Literal["mine", "channel"] = Field(
        default="mine",
        description="List reminders created by this user ('mine') or all in this channel ('channel').",
    )


class CancelReminderArgs(BaseModel):
    """Args for cancelling a reminder."""

    reminder_id: int = Field(
        ...,
        description="The reminder ID to cancel. Get IDs from list_reminders.",
    )
