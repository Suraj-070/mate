"""Pydantic schemas for the tool framework.

Each tool defines:
  - A *schema*: the JSON schema passed to the LLM describing what the tool does
  - An *args model*: a Pydantic model used to validate the LLM's args before execution
  - A *result*: the structured return value from execution

The LLM never sees the args model directly — only the JSON schema. The args
model exists purely for backend validation.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Structured result of a tool execution.

    The orchestrator appends this as a `tool` role message to the LLM context.
    The LLM must read `success` and relay failures to the user — never claim
    success when success=False.
    """

    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    user_message: Optional[str] = Field(
        default=None,
        description="Optional suggested message for the LLM to relay to the user",
    )

    def to_llm_string(self) -> str:
        """Serialize for injection into the LLM as a tool result message."""
        return json.dumps(
            {
                "success": self.success,
                "data": self.data,
                "error": self.error_message,
                "suggested_message": self.user_message,
            },
            ensure_ascii=False,
            default=str,
        )


class ToolContext(BaseModel):
    """Context passed to every tool's execute() method.

    Contains everything a tool needs to know about the calling user/channel
    so it can do permission checks and route outputs correctly.
    """

    user_db_id: int
    discord_user_id: str
    user_display_name: str
    is_admin: bool

    channel_db_id: int
    discord_channel_id: str
    discord_guild_id: Optional[str] = None


# ── Tool definition (for LLM) ─────────────────────────────────
def make_tool_schema(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Build an OpenAI-compatible tool schema for the LLM."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
