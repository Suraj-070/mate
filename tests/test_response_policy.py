"""Tests for the response decision layer.

This is the most important file for naturalness — we want comprehensive
coverage of the decision matrix.
"""
from __future__ import annotations

from app.ai.response_policy import decide, looks_addressed_to_bot, looks_like_question
from app.ai.schemas import DecisionInput, DecisionOutcome


# ── Helper ────────────────────────────────────────────────────
def _input(**overrides) -> DecisionInput:
    defaults = dict(
        is_direct_mention=False,
        is_reply_to_bot=False,
        is_reply_to_other=False,
        bot_spoke_recently=False,
        is_question=False,
        addressed_to_bot_by_name=False,
        channel_autonomy_enabled=False,
        cooldown_active=False,
        rate_limit_exceeded=False,
        author_opted_out=False,
        message_length=10,
    )
    defaults.update(overrides)
    return DecisionInput(**defaults)


# ── Decision tests ────────────────────────────────────────────
class TestDecide:
    def test_opted_out_user_always_ignored(self):
        result = decide(_input(author_opted_out=True, is_direct_mention=True))
        assert result.outcome == DecisionOutcome.IGNORE

    def test_rate_limited_always_ignored(self):
        result = decide(_input(rate_limit_exceeded=True, is_direct_mention=True))
        assert result.outcome == DecisionOutcome.IGNORE

    def test_cooldown_with_mention_still_responds(self):
        """Direct mention overrides cooldown — user is explicitly addressing bot."""
        result = decide(_input(cooldown_active=True, is_direct_mention=True))
        assert result.outcome == DecisionOutcome.RESPOND

    def test_cooldown_without_mention_is_ignored(self):
        result = decide(_input(cooldown_active=True, is_direct_mention=False))
        assert result.outcome == DecisionOutcome.IGNORE

    def test_direct_mention_triggers_response(self):
        result = decide(_input(is_direct_mention=True))
        assert result.outcome == DecisionOutcome.RESPOND
        assert "mention" in result.reason

    def test_reply_to_bot_triggers_response(self):
        result = decide(_input(is_reply_to_bot=True))
        assert result.outcome == DecisionOutcome.RESPOND
        assert "reply to bot" in result.reason

    def test_addressed_by_name_triggers_response(self):
        result = decide(_input(addressed_to_bot_by_name=True))
        assert result.outcome == DecisionOutcome.RESPOND
        assert "name" in result.reason

    def test_autonomy_off_ignores_unrelated_messages(self):
        """Default behavior: bot only replies when explicitly addressed."""
        result = decide(
            _input(
                channel_autonomy_enabled=False,
                is_question=True,
                bot_spoke_recently=True,
            )
        )
        assert result.outcome == DecisionOutcome.IGNORE

    def test_autonomy_on_question_no_recent_bot_ignored(self):
        """Even with autonomy on, bot only chimes in if recently part of convo."""
        result = decide(
            _input(
                channel_autonomy_enabled=True,
                is_question=True,
                bot_spoke_recently=False,
            )
        )
        assert result.outcome == DecisionOutcome.IGNORE

    def test_autonomy_on_question_recent_bot_responds(self):
        result = decide(
            _input(
                channel_autonomy_enabled=True,
                is_question=True,
                bot_spoke_recently=True,
            )
        )
        assert result.outcome == DecisionOutcome.RESPOND
        assert "autonomous" in result.reason

    def test_autonomy_on_no_question_ignored(self):
        """Even with autonomy + recent activity, no question = no reply."""
        result = decide(
            _input(
                channel_autonomy_enabled=True,
                is_question=False,
                bot_spoke_recently=True,
            )
        )
        assert result.outcome == DecisionOutcome.IGNORE

    def test_priority_mention_beats_rate_limit_but_not_optout(self):
        """Opt-out is highest priority — beats everything."""
        result = decide(
            _input(
                author_opted_out=True,
                is_direct_mention=True,
                rate_limit_exceeded=True,
            )
        )
        assert result.outcome == DecisionOutcome.IGNORE


# ── Question detection ────────────────────────────────────────
class TestLooksLikeQuestion:
    def test_question_mark(self):
        assert looks_like_question("what time is it?") is True

    def test_devanagari_question_mark(self):
        assert looks_like_question("खाना खायौ?") is True

    def test_question_word_at_start(self):
        assert looks_like_question("what time is it") is True
        assert looks_like_question("why are we here") is True
        assert looks_like_question("how does this work") is True

    def test_statement_not_question(self):
        assert looks_like_question("the sky is blue") is False
        assert looks_like_question("yo") is False

    def test_empty_string(self):
        assert looks_like_question("") is False

    def test_romanized_nepali_question_word(self):
        assert looks_like_question("k chha khabar") is True


# ── Addressed-to-bot detection ────────────────────────────────
class TestLooksAddressedToBot:
    def test_name_prefix_comma(self):
        assert looks_addressed_to_bot("bot, what's up") is True

    def test_name_prefix_colon(self):
        assert looks_addressed_to_bot("ai: please help") is True

    def test_custom_bot_name_match(self):
        assert looks_addressed_to_bot("Groupmate, hi", bot_name="Groupmate") is True

    def test_no_prefix(self):
        assert looks_addressed_to_bot("what's up") is False

    def test_word_containing_name_not_matched(self):
        """Don't false-positive on words containing 'ai' etc."""
        assert looks_addressed_to_bot("I'm going to training") is False
