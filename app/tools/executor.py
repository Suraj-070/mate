"""Tool executor — orchestrates validation, permission check, and execution.

The orchestrator (app/ai/orchestrator.py) calls this for every tool_call the
LLM emits. The executor:
  1. Looks up the tool by name (rejects unknown tools)
  2. Validates args against the tool's Pydantic model
  3. Runs permission check
  4. Executes
  5. Returns ToolResult — never raises (errors become ToolResult with success=False)

The LLM never directly executes anything. The executor is the ONLY path
between "LLM wants to do X" and "X happens."
"""
from __future__ import annotations

import json
from typing import Any

from app.tools.registry import get_default_registry
from app.tools.schemas import ToolContext, ToolResult
from app.utils.logging import get_logger

log = get_logger(__name__)


class ToolNotFoundError(Exception):
    pass


async def execute_tool_call(
    tool_name: str,
    raw_args: dict[str, Any] | str,
    ctx: ToolContext,
) -> ToolResult:
    """Validate + check permissions + execute a single tool call.

    NEVER raises — all failures become ToolResult(success=False, error_message=...).
    """
    registry = get_default_registry()
    tool = registry.get(tool_name)
    if tool is None:
        log.warning("tool_not_found", name=tool_name, available=list(registry._tools.keys()))
        return ToolResult(
            success=False,
            error_message=f"unknown tool: {tool_name}",
            user_message=f"I don't have a tool called '{tool_name}'.",
        )

    # ── Validate args ──────────────────────────────────────────
    parsed_args, validation_error = tool.validate_args(raw_args)
    if validation_error is not None:
        log.warning(
            "tool_arg_validation_failed",
            tool=tool_name,
            error=validation_error,
        )
        return ToolResult(
            success=False,
            error_message=f"invalid arguments: {validation_error}",
            user_message="I couldn't quite understand the details for that — can you rephrase?",
        )

    # ── Permission check ─────────────────────────────────────
    try:
        perm_error = tool.check_permissions(ctx, parsed_args)
    except Exception as e:
        log.exception("tool_permission_check_error", tool=tool_name, error=str(e))
        return ToolResult(
            success=False,
            error_message=f"permission check failed: {e}",
            user_message="couldn't verify permissions for that — try again?",
        )

    if perm_error is not None:
        log.info(
            "tool_permission_denied",
            tool=tool_name,
            user=ctx.discord_user_id,
            reason=perm_error,
        )
        return ToolResult(
            success=False,
            error_message=f"permission denied: {perm_error}",
            user_message=f"you can't do that — {perm_error}",
        )

    # ── Execute ────────────────────────────────────────────────
    try:
        result = await tool.execute(parsed_args, ctx)
    except Exception as e:
        log.exception("tool_execution_error", tool=tool_name, error=str(e))
        return ToolResult(
            success=False,
            error_message=f"execution failed: {e}",
            user_message="that didn't work — something went wrong on my end.",
        )

    log.info(
        "tool_executed",
        tool=tool_name,
        success=result.success,
        user=ctx.discord_user_id,
        channel=ctx.discord_channel_id,
    )
    return result
