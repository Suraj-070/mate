"""Tests for the context builder.

Verifies token budgeting, message grouping, current-message handling.
Phase 2: tests updated for the new async build_request signature with
channel_db_id + discord_channel_id args.
"""
from __future__ import annotations

import asyncio
import time

from app.ai.schemas import LLMMessage
from app.config.settings import get_settings
from app.context.builder import build_request, invalidate_style_cache
from app.context.conversation_buffer import BufferedMessage, get_buffer


def _make_msg(
    msg_id: str,
    author_id: str,
    author_name: str,
    content: str,
    is_bot: bool = False,
) -> BufferedMessage:
    return BufferedMessage(
        discord_message_id=msg_id,
        author_id=author_id,
        author_name=author_name,
        content=content,
        is_bot=is_bot,
        timestamp=time.time(),
    )


class TestBuildRequest:
    def test_returns_request_with_system_prompt(self):
        async def _run():
            buffer = get_buffer()
            current = _make_msg("100", "u1", "Alice", "hi bot")
            buffer.append("ch1", current)
            request = await build_request(
                channel_db_id=1,
                discord_channel_id="ch1",
                current_message=current,
            )
            assert request.system_prompt
            assert "IDENTITY" in request.system_prompt
            assert request.messages  # at least the current message
        asyncio.run(_run())

    def test_current_message_always_included(self):
        async def _run():
            buffer = get_buffer()
            current = _make_msg("100", "u1", "Alice", "what's up?")
            buffer.append("ch1", current)
            request = await build_request(1, "ch1", current)
            last = request.messages[-1]
            assert last.role == "user"
            assert "[now]" in (last.content or "")
        asyncio.run(_run())

    def test_consecutive_same_author_grouped(self):
        """Two messages from the same author should be combined into one user turn."""
        async def _run():
            buffer = get_buffer()
            m1 = _make_msg("1", "u1", "Alice", "hey")
            m2 = _make_msg("2", "u1", "Alice", "anyone there?")
            m3 = _make_msg("3", "u2", "Bob", "yeah")
            current = _make_msg("4", "u1", "Alice", "cool")
            for m in (m1, m2, m3, current):
                buffer.append("ch1", m)
            request = await build_request(1, "ch1", current)
            user_turns = [m for m in request.messages if m.role == "user"]
            assert len(user_turns) <= 3  # grouped
            assert "hey" in (user_turns[0].content or "")
            assert "anyone there?" in (user_turns[0].content or "")
        asyncio.run(_run())

    def test_bot_messages_become_assistant_turns(self):
        async def _run():
            buffer = get_buffer()
            m1 = _make_msg("1", "u1", "Alice", "hi")
            m2 = _make_msg("2", "bot", "Bot", "hey", is_bot=True)
            current = _make_msg("3", "u1", "Alice", "what's up?")
            for m in (m1, m2, current):
                buffer.append("ch1", m)
            request = await build_request(1, "ch1", current)
            assistant_turns = [m for m in request.messages if m.role == "assistant"]
            assert len(assistant_turns) == 1
            assert assistant_turns[0].content == "hey"
        asyncio.run(_run())

    def test_token_budget_enforced(self):
        async def _run():
            settings = get_settings()
            original = settings.context_max_tokens
            settings.__dict__["context_max_tokens"] = 50  # tiny budget
            try:
                buffer = get_buffer()
                for i in range(10):
                    m = _make_msg(str(i), "u1", "Alice", f"message {i} " + "x" * 100)
                    buffer.append("ch1", m)
                current = _make_msg("99", "u1", "Alice", "current")
                buffer.append("ch1", current)
                request = await build_request(1, "ch1", current)
                assert len(request.messages) < 12
                assert any("[now]" in (m.content or "") for m in request.messages)
            finally:
                settings.__dict__["context_max_tokens"] = original
        asyncio.run(_run())

    def test_empty_channel(self):
        async def _run():
            buffer = get_buffer()
            current = _make_msg("1", "u1", "Alice", "first message")
            buffer.append("ch1", current)
            request = await build_request(1, "ch1", current)
            assert len(request.messages) == 1
            assert request.messages[0].role == "user"
        asyncio.run(_run())

    def test_tools_auto_injected_when_enabled(self):
        """Phase 3: tools are auto-loaded from registry when enable_tools=True."""
        async def _run():
            buffer = get_buffer()
            current = _make_msg("1", "u1", "Alice", "hi")
            buffer.append("ch1", current)
            request = await build_request(1, "ch1", current)
            # Tools should be present (create_reminder, list_reminders, etc.)
            assert len(request.tools) > 0
            tool_names = [t["function"]["name"] for t in request.tools]
            assert "create_reminder" in tool_names
            assert "start_timer" in tool_names
        asyncio.run(_run())

    def test_system_prompt_contains_language_rules(self):
        async def _run():
            buffer = get_buffer()
            current = _make_msg("1", "u1", "Alice", "hi")
            buffer.append("ch1", current)
            request = await build_request(1, "ch1", current)
            assert "LANGUAGE BEHAVIOR" in request.system_prompt
            assert "Devanagari" in request.system_prompt
        asyncio.run(_run())

    def test_style_cache_invalidation(self):
        """invalidate_style_cache should remove the cached profile."""
        # Just verify the function works without error
        invalidate_style_cache(channel_db_id=999)
        invalidate_style_cache()  # clears all
        # No assertion needed — function should not raise
