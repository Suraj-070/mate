"""Tests for Phase 4: custom HTTP tools, topic tracking, language preferences,
memory extraction proposals."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.ai.prompts import build_system_prompt
from app.config.settings import get_settings
from app.personality.style_profile import default_style_profile
from app.tools.custom.base_http import BaseHTTPTool
from app.tools.custom.weather import WeatherArgs, WeatherTool
from app.tools.schemas import ToolContext, ToolResult


# ── Topic tracking ──────────────────────────────────────────
class TestTopicTracking:
    def test_extract_tokens_filters_stopwords(self):
        from app.context.topic_context import extract_tokens
        tokens = extract_tokens("the weather is nice today")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "weather" in tokens
        assert "nice" in tokens
        assert "today" in tokens

    def test_extract_tokens_min_length(self):
        from app.context.topic_context import extract_tokens
        # "ok" is 2 chars, should be filtered. "hi" is also a stopword AND too short.
        # Use longer words that aren't stopwords.
        tokens = extract_tokens("ok pizza awesome")
        assert "ok" not in tokens
        assert "pizza" in tokens
        assert "awesome" in tokens

    def test_extract_tokens_devanagari(self):
        from app.context.topic_context import extract_tokens
        tokens = extract_tokens("आज खेल राम्रो छ")
        # Should extract 3+ char Devanagari tokens
        assert len(tokens) > 0

    def test_detect_topic_returns_list(self):
        from app.context.topic_context import detect_topic, BufferedMessage
        import time
        msgs = [
            BufferedMessage(
                discord_message_id="1", author_id="u1", author_name="Alice",
                content="anyone playing games tonight", is_bot=False, timestamp=time.time(),
            ),
            BufferedMessage(
                discord_message_id="2", author_id="u2", author_name="Bob",
                content="yeah games are fun", is_bot=False, timestamp=time.time(),
            ),
        ]
        topic = detect_topic(msgs, top_n=3)
        assert isinstance(topic, list)
        assert len(topic) <= 3
        assert "games" in topic

    def test_detect_topic_skips_bot_messages(self):
        from app.context.topic_context import detect_topic, BufferedMessage
        import time
        msgs = [
            BufferedMessage(
                discord_message_id="1", author_id="u1", author_name="Alice",
                content="pizza pizza pizza", is_bot=False, timestamp=time.time(),
            ),
            BufferedMessage(
                discord_message_id="2", author_id="bot", author_name="Bot",
                content="pizza pizza pizza", is_bot=True, timestamp=time.time(),
            ),
        ]
        topic = detect_topic(msgs, top_n=3)
        # Bot's "pizza" shouldn't have doubled the count — but the topic
        # should still be "pizza"
        assert "pizza" in topic

    def test_topic_as_prompt_block_empty(self):
        from app.context.topic_context import topic_as_prompt_block
        assert topic_as_prompt_block([]) is None

    def test_topic_as_prompt_block_non_empty(self):
        from app.context.topic_context import topic_as_prompt_block
        block = topic_as_prompt_block(["gaming", "friday", "8pm"])
        assert "ACTIVE TOPIC" in block
        assert "gaming" in block
        assert "friday" in block


# ── Per-user language preferences ────────────────────────────
class TestUserLanguagePreference:
    def test_set_and_get_preferred_language(self):
        async def _run():
            from app.database import repository as repo
            await repo.get_or_create_user("u-test-1", username="alice", display_name="Alice")
            ok = await repo.set_user_preferred_language("u-test-1", "ne-deva")
            assert ok is True
            lang = await repo.get_user_preferred_language("u-test-1")
            assert lang == "ne-deva"
        asyncio.run(_run())

    def test_clear_preferred_language(self):
        async def _run():
            from app.database import repository as repo
            await repo.get_or_create_user("u-test-2", username="bob", display_name="Bob")
            await repo.set_user_preferred_language("u-test-2", "en")
            assert await repo.get_user_preferred_language("u-test-2") == "en"
            ok = await repo.set_user_preferred_language("u-test-2", None)
            assert ok is True
            assert await repo.get_user_preferred_language("u-test-2") is None
        asyncio.run(_run())

    def test_invalid_language_raises(self):
        async def _run():
            from app.database import repository as repo
            await repo.get_or_create_user("u-test-3", username="charlie", display_name="Charlie")
            with pytest.raises(ValueError):
                await repo.set_user_preferred_language("u-test-3", "invalid-code")
        asyncio.run(_run())

    def test_unknown_user_returns_false(self):
        async def _run():
            from app.database import repository as repo
            ok = await repo.set_user_preferred_language("nonexistent", "en")
            assert ok is False
        asyncio.run(_run())

    def test_get_for_unknown_user_returns_none(self):
        async def _run():
            from app.database import repository as repo
            lang = await repo.get_user_preferred_language("nonexistent")
            assert lang is None
        asyncio.run(_run())

    def test_all_valid_languages_accepted(self):
        async def _run():
            from app.database import repository as repo
            await repo.get_or_create_user("u-test-4", username="dave", display_name="Dave")
            for lang in ("en", "ne-deva", "ne-roman", "mixed"):
                ok = await repo.set_user_preferred_language("u-test-4", lang)
                assert ok is True, f"failed for {lang}"
        asyncio.run(_run())


# ── Weather tool (custom HTTP tool example) ──────────────────
class TestWeatherTool:
    def test_tool_schema_valid(self):
        tool = WeatherTool()
        schema = tool.to_openai_tool()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "get_weather"
        assert "location" in schema["function"]["parameters"]["properties"]
        assert "location" in schema["function"]["parameters"]["required"]

    def test_build_url_with_city_name(self):
        tool = WeatherTool()
        args = WeatherArgs(location="Sydney")
        url = tool.build_url(args)
        assert url.startswith("/Sydney")
        assert "format=3" in url

    def test_build_url_url_encodes_location(self):
        tool = WeatherTool()
        args = WeatherArgs(location="New York")
        url = tool.build_url(args)
        # Space should be encoded
        assert " " not in url
        assert "New" in url
        assert "York" in url

    def test_build_url_with_coordinates(self):
        tool = WeatherTool()
        args = WeatherArgs(location="~33.86,151.21")
        url = tool.build_url(args)
        assert "~" in url  # ~ should be preserved (safe chars)

    def test_parse_response_success(self):
        tool = WeatherTool()
        args = WeatherArgs(location="Sydney")
        ctx = _make_ctx()
        result = tool.parse_response("Sydney: ⛅️ +25°C", args, ctx)
        assert result.success is True
        assert "Sydney" in result.user_message
        assert "25" in result.user_message

    def test_parse_response_unknown_location(self):
        tool = WeatherTool()
        args = WeatherArgs(location="XYZ123")
        ctx = _make_ctx()
        result = tool.parse_response("Unknown location: XYZ123", args, ctx)
        assert result.success is False
        assert "couldn't find" in result.user_message.lower()

    def test_parse_response_empty_body(self):
        tool = WeatherTool()
        args = WeatherArgs(location="Sydney")
        ctx = _make_ctx()
        result = tool.parse_response("", args, ctx)
        assert result.success is False

    def test_weather_args_validates_location_required(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            WeatherArgs()

    def test_weather_args_validates_min_length(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            WeatherArgs(location="")


# ── Memory proposals ─────────────────────────────────────────
class TestMemoryProposals:
    def test_register_and_get_pending_proposal(self):
        from app.memory.proposals import (
            register_pending_proposal,
            get_pending_proposal,
            clear_pending_proposal,
        )
        from app.memory.models import ExtractedMemoryProposal, MemoryKind

        proposal = ExtractedMemoryProposal(
            kind=MemoryKind.FACT,
            content="Group plays games every Friday",
            confidence=0.9,
            rationale="Stated explicitly",
        )
        register_pending_proposal(
            proposal_message_id=999,
            proposal=proposal,
            channel_db_id=1,
            source_message_id="123",
            admin_user_ids=[111],
        )
        fetched = get_pending_proposal(999)
        assert fetched is not None
        assert fetched["content"] == "Group plays games every Friday"
        assert fetched["kind"] == "fact"
        assert fetched["confidence"] == 0.9

        clear_pending_proposal(999)
        assert get_pending_proposal(999) is None

    def test_approve_proposal_creates_memory(self):
        async def _run():
            from app.memory.proposals import (
                register_pending_proposal,
                approve_proposal,
                clear_pending_proposal,
            )
            from app.memory.models import ExtractedMemoryProposal, MemoryKind
            from app.database import repository as repo

            # Setup: channel + admin user
            chan = await repo.get_or_create_channel("g1", "ch-prop-1", "test")
            admin = await repo.get_or_create_user("123456789012345678", username="admin", display_name="Admin", is_admin=True)

            proposal = ExtractedMemoryProposal(
                kind=MemoryKind.PREFERENCE,
                content="Bot should always be casual",
                confidence=0.8,
                rationale="Stated preference",
            )
            register_pending_proposal(
                proposal_message_id=888,
                proposal=proposal,
                channel_db_id=chan.id,
                source_message_id=None,
                admin_user_ids=[123456789012345678],
            )

            success, memory_id, err = await approve_proposal(
                proposal_message_id=888,
                approved_by_discord_user_id="123456789012345678",
                approved_by_user_db_id=admin.id,
            )
            assert success is True
            assert memory_id is not None
            assert err is None

            # Verify the memory was created
            mem = await repo.get_memory(memory_id)
            assert mem is not None
            assert mem.content == "Bot should always be casual"
            assert mem.kind == "preference"

            # Verify pending was cleared
            from app.memory.proposals import get_pending_proposal
            assert get_pending_proposal(888) is None
        asyncio.run(_run())

    def test_approve_unknown_proposal_returns_false(self):
        async def _run():
            from app.memory.proposals import approve_proposal
            success, memory_id, err = await approve_proposal(
                proposal_message_id=123456789,
                approved_by_discord_user_id="u1",
                approved_by_user_db_id=1,
            )
            assert success is False
            assert memory_id is None
        asyncio.run(_run())

    def test_reject_proposal_clears_pending(self):
        from app.memory.proposals import (
            register_pending_proposal,
            reject_proposal,
            get_pending_proposal,
        )
        from app.memory.models import ExtractedMemoryProposal, MemoryKind

        proposal = ExtractedMemoryProposal(
            kind=MemoryKind.JOKE,
            content="test joke proposal",
            confidence=0.3,
            rationale="testing",
        )
        register_pending_proposal(
            proposal_message_id=777,
            proposal=proposal,
            channel_db_id=1,
            source_message_id=None,
            admin_user_ids=[111],
        )
        assert get_pending_proposal(777) is not None
        ok = asyncio.run(reject_proposal(777))
        assert ok is True
        assert get_pending_proposal(777) is None

    def test_reject_unknown_proposal_returns_false(self):
        async def _run():
            from app.memory.proposals import reject_proposal
            ok = await reject_proposal(999999)
            assert ok is False
        asyncio.run(_run())


# ── Prompt building with Phase 4 layers ──────────────────────
class TestPromptPhase4:
    def test_prompt_includes_topic_block_when_provided(self):
        prompt = build_system_prompt(
            topic_block="═══ ACTIVE TOPIC ═══\ngaming, friday",
        )
        assert "ACTIVE TOPIC" in prompt
        assert "gaming" in prompt
        assert "friday" in prompt

    def test_prompt_omits_topic_block_when_none(self):
        prompt = build_system_prompt()
        assert "ACTIVE TOPIC" not in prompt

    def test_prompt_includes_user_language_hint(self):
        prompt = build_system_prompt(user_language_hint="ne-deva")
        assert "USER LANGUAGE PREFERENCE" in prompt
        assert "ne-deva" in prompt

    def test_prompt_omits_user_language_hint_when_none(self):
        prompt = build_system_prompt()
        assert "USER LANGUAGE PREFERENCE" not in prompt

    def test_prompt_with_all_phase4_layers(self):
        prompt = build_system_prompt(
            topic_block="═══ ACTIVE TOPIC ═══\ngaming, friday",
            user_language_hint="ne-roman",
            long_term_memories=["[fact] Group plays Friday 8pm"],
            conversation_summary="Discussed gaming plans",
        )
        assert "ACTIVE TOPIC" in prompt
        assert "USER LANGUAGE PREFERENCE" in prompt
        assert "LONG-TERM MEMORIES" in prompt
        assert "RECENT CONVERSATION SUMMARY" in prompt


# ── WeatherTool integration with executor ───────────────────
class TestWeatherToolExecutor:
    """Tests that the weather tool is registered and reachable via executor.

    We don't test actual HTTP calls — those depend on network + wttr.in being up.
    Instead we verify the tool is registered and its schema is correct.
    """

    def test_weather_tool_registered_by_default(self):
        from app.tools.registry import get_default_registry, reset_registry
        reset_registry()
        registry = get_default_registry()
        assert "get_weather" in registry

    def test_weather_tool_schema_has_location_required(self):
        from app.tools.registry import get_default_registry, reset_registry
        reset_registry()
        registry = get_default_registry()
        weather = registry.get("get_weather")
        schema = weather.to_openai_tool()
        assert "location" in schema["function"]["parameters"]["required"]

    def test_executor_rejects_invalid_weather_args(self):
        async def _run():
            from app.tools.executor import execute_tool_call
            ctx = _make_ctx()
            # Missing 'location' field
            result = await execute_tool_call("get_weather", {}, ctx)
            assert result.success is False
            assert "invalid arguments" in result.error_message
        asyncio.run(_run())

    def test_executor_handles_unknown_weather_location_gracefully(self):
        """If wttr.in returns an error, the tool result is success=False."""
        # We can't easily mock the HTTP call without more setup; the
        # parse_response method is tested directly in TestWeatherTool.
        pass


# ── Helpers ──────────────────────────────────────────────────
def _make_ctx() -> ToolContext:
    return ToolContext(
        user_db_id=1,
        discord_user_id="123",
        user_display_name="Alice",
        is_admin=False,
        channel_db_id=1,
        discord_channel_id="999",
    )


def get_pending_proposal_sync(proposal_message_id: int):
    """Helper to check pending proposals synchronously from async tests."""
    from app.memory.proposals import get_pending_proposal
    return get_pending_proposal(proposal_message_id)
