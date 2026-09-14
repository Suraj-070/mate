"""AI orchestrator — owns the LLM call loop + tool dispatch.

Loop:
  1. Build LLMRequest (caller does this)
  2. Call provider.generate()
  3. If response has tool_calls:
     - For each tool_call: validate args, check permissions, execute
     - Append tool result as `tool` role message
     - Re-call provider with updated context
     - Loop max `tool_max_iterations` times (default 3)
  4. Return final content

Critical invariants:
  - LLM NEVER executes tools directly. The orchestrator dispatches every call.
  - LLM NEVER sees DB credentials or shell access.
  - If a tool returns success=False, the LLM is told — it must NOT claim success.
  - Loop is bounded — runaway tool loops don't hang the bot.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.ai.provider import ProviderError, get_provider
from app.ai.schemas import LLMMessage, LLMRequest, LLMResponse
from app.config.settings import get_settings
from app.tools.executor import execute_tool_call
from app.tools.registry import get_default_registry
from app.tools.schemas import ToolContext, ToolResult
from app.utils.logging import get_logger

log = get_logger(__name__)


async def generate_response(
    request: LLMRequest,
    tool_context: ToolContext | None = None,
) -> str:
    """Generate the final user-facing reply text.

    Phase 1: single-shot, no tools.
    Phase 3: full tool-call loop with bounded iterations.

    Args:
        request: assembled LLMRequest (system_prompt + messages + tools)
        tool_context: if tools are registered, the calling user/channel context
                      for permission checks. Required if request.tools is non-empty.

    Returns the final text reply.
    """
    settings = get_settings()
    provider = get_provider()
    max_iter = settings.tool_max_iterations if settings.enable_tools else 1

    # ── Iteration loop ─────────────────────────────────────────
    for iteration in range(max_iter):
        try:
            response: LLMResponse = await provider.generate(request)
        except ProviderError as e:
            log.error("orchestrator_provider_error", error=str(e), iteration=iteration)
            return "_(couldn't reach my brain right now — try again in a sec)_"

        log.info(
            "llm_response",
            iteration=iteration,
            prompt_tokens=response.usage_prompt_tokens,
            completion_tokens=response.usage_completion_tokens,
            finish_reason=response.finish_reason,
            content_len=len(response.content or ""),
            tool_calls=len(response.tool_calls),
        )

        # ── No tool calls? We're done. ────────────────────────
        if not response.has_tool_calls or not settings.enable_tools:
            content = (response.content or "").strip()
            if not content and iteration > 0:
                # LLM may have ended without saying anything after a tool call.
                # Fall back to a generic ack.
                return "done"
            return content

        # ── Tool calls present. Execute each, append result, loop. ──
        if tool_context is None:
            log.warning("orchestrator_tool_calls_without_context", count=len(response.tool_calls))
            # Tell the LLM it needs to give a textual reply, not tool calls
            request.messages.append(
                LLMMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            request.messages.append(
                LLMMessage(
                    role="user",
                    content="(note: tools are not available in this context — please respond with text only)",
                )
            )
            continue

        # Add the assistant turn with tool calls (so the LLM can see what it asked for)
        request.messages.append(
            LLMMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        # Execute each tool call and append the result
        for tc in response.tool_calls:
            tool_call_id = tc.get("id", "")
            function_block = tc.get("function", {})
            tool_name = function_block.get("name", "")

            # Sanitize tool name — some models hallucinate suffixes like
            # "list_reminders<|channel|>commentary". Strip at first non-alnum/underscore char.
            tool_name = re.split(r"[^a-zA-Z0-9_]", tool_name)[0]

            raw_args = function_block.get("arguments", {})

            log.info(
                "tool_call_dispatched",
                tool=tool_name,
                iteration=iteration,
                call_id=tool_call_id,
            )

            result: ToolResult = await execute_tool_call(tool_name, raw_args, tool_context)

            # Append the tool result as a `tool` role message
            request.messages.append(
                LLMMessage(
                    role="tool",
                    content=result.to_llm_string(),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
            )

        # Loop back to top — provider.generate() will be called again with the
        # updated context (now including tool results).

    # ── Exceeded max iterations ────────────────────────────────
    log.warning("orchestrator_max_iterations_reached", max=max_iter)
    return "_(I got a bit tangled up trying to handle that — could you rephrase?)_"
