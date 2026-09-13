"""Tests for the memory subsystem (Phase 2)."""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from app.ai.schemas import DecisionInput, DecisionOutcome
from app.ai.response_policy import decide, looks_like_acknowledgement
from app.bot.reactions import pick_reaction
from app.memory.models import MemoryKind, MemoryCreate
from app.memory.policies import (
    can_use_message_for_style,
    can_use_message_for_memory,
    looks_sensitive,
    default_expiry_for_kind,
    default_confidence_for_kind,
)
from app.personality.style_profile import (
    StyleProfile,
    default_style_profile,
    StyleProfile as SP,
)


# ── Decision policy: REACT outcome ────────────────────────────
class TestReactOutcome:
    def test_short_acknowledgement_mention_triggers_react(self):
        from app.ai.response_policy import decide
        result = decide(
            DecisionInput(
                is_direct_mention=True,
                is_question=False,
                message_text="ok",
                enable_react_outcome=True,
            )
        )
        assert result.outcome == DecisionOutcome.REACT

    def test_long_mention_does_not_react(self):
        from app.ai.response_policy import decide
        result = decide(
            DecisionInput(
                is_direct_mention=True,
                is_question=False,
                message_text="hey bot, can you explain what time we're meeting tonight and where",
                enable_react_outcome=True,
            )
        )
        assert result.outcome == DecisionOutcome.RESPOND

    def test_question_mention_does_not_react(self):
        from app.ai.response_policy import decide
        result = decide(
            DecisionInput(
                is_direct_mention=True,
                is_question=True,
                message_text="what time?",
                enable_react_outcome=True,
            )
        )
        assert result.outcome == DecisionOutcome.RESPOND

    def test_react_disabled_falls_back_to_respond(self):
        from app.ai.response_policy import decide
        result = decide(
            DecisionInput(
                is_direct_mention=True,
                is_question=False,
                message_text="ok",
                enable_react_outcome=False,
            )
        )
        assert result.outcome == DecisionOutcome.RESPOND


# ── Acknowledgement detection ─────────────────────────────────
class TestLooksLikeAcknowledgement:
    def test_ok(self):
        assert looks_like_acknowledgement("ok") is True

    def test_cool(self):
        assert looks_like_acknowledgement("cool") is True

    def test_thanks(self):
        assert looks_like_acknowledgement("thanks") is True

    def test_yo(self):
        assert looks_like_acknowledgement("yo") is True

    def test_question_not_ack(self):
        assert looks_like_acknowledgement("what time is it?") is False

    def test_long_message_not_ack(self):
        assert looks_like_acknowledgement("this is a really long message that goes on and on about many things") is False


# ── Reaction picker ────────────────────────────────────────────
class TestPickReaction:
    def test_lol_gets_laugh(self):
        assert pick_reaction("lol that's hilarious") == "😂"

    def test_game_gets_controller(self):
        assert pick_reaction("let's play a game") == "🎮"

    def test_ugh_gets_sweat(self):
        assert pick_reaction("ugh long day") == "😅"

    def test_default_for_unknown(self):
        emoji = pick_reaction("random unrelated message")
        assert emoji in ["👍", "👌", "🫡"]

    def test_empty_returns_default(self):
        emoji = pick_reaction("")
        assert emoji in ["👍", "👌", "🫡"]


# ── Privacy policies ──────────────────────────────────────────
class TestPrivacyPolicies:
    def test_opted_out_user_cannot_be_style_sampled(self):
        from app.database.models import Message, User
        msg = Message(
            id=1, channel_id=1, discord_message_id="123",
            author_id=1, content="hello", is_bot=False,
        )
        author = User(
            id=1, discord_user_id="123", username="alice",
            display_name="Alice", is_admin=False,
            opt_out_style=True, opt_out_memory=False, opt_out_reply=False,
        )
        assert can_use_message_for_style(msg, author) is False

    def test_bot_message_cannot_be_style_sampled(self):
        from app.database.models import Message, User
        msg = Message(
            id=1, channel_id=1, discord_message_id="123",
            author_id=1, content="hello", is_bot=True,
        )
        author = User(
            id=1, discord_user_id="123", username="bot",
            display_name="Bot", is_admin=False,
            opt_out_style=False, opt_out_memory=False, opt_out_reply=False,
        )
        assert can_use_message_for_style(msg, author) is False

    def test_normal_user_can_be_sampled(self):
        from app.database.models import Message, User
        msg = Message(
            id=1, channel_id=1, discord_message_id="123",
            author_id=1, content="hello world", is_bot=False,
        )
        author = User(
            id=1, discord_user_id="123", username="alice",
            display_name="Alice", is_admin=False,
            opt_out_style=False, opt_out_memory=False, opt_out_reply=False,
        )
        assert can_use_message_for_style(msg, author) is True


# ── Sensitive content detection ───────────────────────────────
class TestLooksSensitive:
    def test_phone_number(self):
        assert looks_sensitive("call me at +1 555 123 4567") is True

    def test_email(self):
        assert looks_sensitive("email me at alice@example.com") is True

    def test_normal_message(self):
        assert looks_sensitive("yo anyone playing tonight?") is False

    def test_devanagari_message(self):
        assert looks_sensitive("नमस्ते संसार") is False

    def test_long_token_like_string(self):
        assert looks_sensitive("MTIzNDU2Nzg5MDEyMzQ1Njc4OWFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6") is True


# ── Memory expiry defaults ────────────────────────────────────
class TestExpiryDefaults:
    def test_fact_never_expires(self):
        assert default_expiry_for_kind("fact") is None

    def test_lore_never_expires(self):
        assert default_expiry_for_kind("lore") is None

    def test_event_expires_in_30_days(self):
        expiry = default_expiry_for_kind("event")
        assert expiry is not None
        delta = expiry - datetime.now(timezone.utc)
        assert 29 <= delta.days <= 31

    def test_joke_expires_in_180_days(self):
        expiry = default_expiry_for_kind("joke")
        assert expiry is not None
        delta = expiry - datetime.now(timezone.utc)
        assert 179 <= delta.days <= 181

    def test_unknown_kind_returns_none(self):
        assert default_expiry_for_kind("unknown") is None


# ── Confidence defaults ──────────────────────────────────────
class TestConfidenceDefaults:
    def test_fact_has_highest_confidence(self):
        assert default_confidence_for_kind("fact") == 1.0

    def test_joke_has_lowest_confidence(self):
        assert default_confidence_for_kind("joke") == 0.3

    def test_unknown_kind_default(self):
        assert default_confidence_for_kind("unknown") == 0.5


# ── Style profile JSON round-trip ─────────────────────────────
class TestStyleProfileSerialization:
    def test_to_json_and_back_preserves_data(self):
        original = StyleProfile(
            formality="casual",
            average_response_length="medium",
            humor_level="high",
            emoji_usage="frequent",
            language_behavior="mixed",
            common_expressions=["yo", "lol", "facts"],
            style_examples=["hey what's up", "haha true"],
        )
        json_str = original.to_json()
        restored = StyleProfile.from_json(json_str)
        assert restored.formality == "casual"
        assert restored.average_response_length == "medium"
        assert restored.humor_level == "high"
        assert restored.emoji_usage == "frequent"
        assert restored.language_behavior == "mixed"
        assert restored.common_expressions == ["yo", "lol", "facts"]
        assert restored.style_examples == ["hey what's up", "haha true"]

    def test_from_json_invalid_returns_default(self):
        restored = StyleProfile.from_json("not valid json")
        default = default_style_profile()
        assert restored.formality == default.formality
        assert restored.humor_level == default.humor_level

    def test_from_json_partial_data_uses_defaults(self):
        restored = StyleProfile.from_json('{"formality": "formal"}')
        assert restored.formality == "formal"
        assert restored.humor_level == "moderate"  # default

    def test_as_prompt_block_includes_all_fields(self):
        profile = StyleProfile(
            formality="very_casual",
            average_response_length="short",
            humor_level="moderate",
            emoji_usage="occasional",
            language_behavior="mixed",
            common_expressions=["yo"],
            style_examples=["hey"],
        )
        block = profile.as_prompt_block()
        assert "very_casual" in block
        assert "short" in block
        assert "moderate" in block
        assert "occasional" in block
        assert "mixed" in block
        assert "yo" in block
        assert "hey" in block
