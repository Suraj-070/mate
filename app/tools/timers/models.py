"""Pydantic schemas for the timers tool."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class StartTimerArgs(BaseModel):
    """Args for starting a timer.

    Timers are short-duration (minutes/hours). The LLM passes `duration` as a
    natural-language string. We parse it server-side and compute expires_at.
    """

    duration: str = Field(
        ...,
        description="How long the timer should run. Natural language OK. "
                    "Examples: '10 minutes', '5m', '1 hour', '90 seconds', '2h30m'.",
    )
    label: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Optional label shown when the timer fires.",
    )


class ListTimersArgs(BaseModel):
    """Args for listing timers."""

    scope: str = Field(
        default="mine",
        description="'mine' or 'channel'. Default mine.",
    )


class CancelTimerArgs(BaseModel):
    """Args for cancelling a timer."""

    timer_id: int = Field(..., description="The timer ID to cancel.")
