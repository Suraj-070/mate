"""Response decision layer — decides whether the bot should reply at all.

This is THE most important file for naturalness. Without it, the bot is
"yet another reply-on-everything assistant." With it, the bot has social
awareness: it knows when to reply, when to react, and when to stay silent.

Phase 1: rule-based.
Phase 2:
  - REACT outcome added — bot may emoji-react instead of replying when
    mentioned but the message is just an acknowledgement.
  - LLM-assist hook added (behind feature flag) for gray-zone autonomous
    participation decisions.

Decision priority order (highest first):
  1. Author opted out of replies         → IGNORE
  2. Rate limit exceeded                  → IGNORE
  3. Cooldown active AND not direct mention → IGNORE
  4. Direct mention                       → check if REACT or RESPOND
  5. Reply to bot's message               → RESPOND
  6. Addressed to bot by name             → RESPOND
  7. Channel autonomy OFF                 → IGNORE
  8. Channel autonomy ON + is_question + bot_spoke_recently → RESPOND
  9. Otherwise                            → IGNORE

Note: EXECUTE_TOOL is never directly returned here. The decision is RESPOND,
and the orchestrator handles tool-call detection from the LLM output.

REACT heuristic: if the message is a direct mention but very short AND looks
like an acknowledgement (no question, no request), the bot can react instead
of replying. This avoids the "say hi to be polite" trap.
"""
from __future__ import annotations

import re
from typing import Optional

from app.ai.schemas import DecisionInput, DecisionOutcome, DecisionResult
from app.utils.logging import get_logger

log = get_logger(__name__)


# Heuristic: a message is "addressed to bot by name" if it starts with
# "bot," / "ai," / "groupmate," (case-insensitive, optional punctuation).
# The bot's actual name is also checked at runtime in message_handler.
_NAME_PATTERNS = [
    re.compile(r"^(bot|ai|groupmate|b|ai)\s*[,:]\s*", re.IGNORECASE),
]

# Question heuristic: ends with '?' OR starts with a question word.
_QUESTION_STARTERS = {
    "what", "why", "how", "when", "where", "who", "which", "whose",
    "can", "could", "would", "should", "do", "does", "did", "is", "are",
    "am", "will", "have", "has", "may", "might", "shall",
    # Nepali romanized
    "k", "ke", "kina", "kaha", "kaha", "kasari", "katile",
}

# Phrases that look like acknowledgements rather than things needing a reply.
# Used by the REACT heuristic.
_ACK_PATTERNS = [
    re.compile(r"^(ok|okay|k|cool|nice|gotcha|got it|sure|yeah|yep|nope|no|haha|lol|lmao|true|facts|real)\b", re.IGNORECASE),
    re.compile(r"^(thanks|thank you|thx|ty)\b", re.IGNORECASE),
    re.compile(r"^(yo|hey|hi|hello|sup|namaste|नमस्ते)\b", re.IGNORECASE),
]


def looks_like_question(text: str) -> bool:
    """Cheap heuristic — used only for the decision layer, not for the LLM."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first_word = stripped.split()[0].lower().rstrip(",.!?:")
    return first_word in _QUESTION_STARTERS


def looks_addressed_to_bot(text: str, bot_name: str | None = None) -> bool:
    """Check if the message opens with a name-pattern addressing the bot."""
    for pat in _NAME_PATTERNS:
        if pat.search(text):
            return True
    if bot_name:
        prefix = bot_name.lower()
        if text.lower().startswith(prefix):
            rest = text[len(prefix):]
            if rest and (rest[0] in " ,:!\n\t"):
                return True
    return False


def looks_like_acknowledgement(text: str) -> bool:
    """Check if the message looks like a brief acknowledgement.

    Used by the REACT heuristic to decide: should we reply or just react?
    Acknowledgements get a reaction, not a reply, to avoid the "say hi
    to be polite" trap.
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Very short messages under ~30 chars are candidates
    if len(stripped) > 60:
        return False
    # If it's a question, it's NOT an acknowledgement
    if looks_like_question(stripped):
        return False
    for pat in _ACK_PATTERNS:
        if pat.match(stripped):
            return True
    return False


def decide(inp: DecisionInput) -> DecisionResult:
    """Apply rule-based decision. See module docstring for priority order."""

    # 1. Opted out
    if inp.author_opted_out:
        return DecisionResult(
            outcome=DecisionOutcome.IGNORE,
            reason="author opted out of bot replies",
        )

    # 2. Rate limit
    if inp.rate_limit_exceeded:
        return DecisionResult(
            outcome=DecisionOutcome.IGNORE,
            reason="channel rate limit exceeded",
        )

    # 3. Cooldown (unless explicitly addressed)
    if inp.cooldown_active and not (inp.is_direct_mention or inp.is_reply_to_bot):
        return DecisionResult(
            outcome=DecisionOutcome.IGNORE,
            reason="cooldown active and not directly addressed",
        )

    # 4. Direct mention — decide between REACT and RESPOND
    if inp.is_direct_mention:
        # REACT heuristic: short acknowledgement-style messages get a reaction
        # rather than a contentless "hey!" reply.
        if (
            inp.enable_react_outcome
            and not inp.is_question
            and looks_like_acknowledgement(getattr(inp, "message_text", ""))
        ):
            return DecisionResult(
                outcome=DecisionOutcome.REACT,
                reason="direct mention + short acknowledgement → react instead of reply",
                debug={"trigger": "mention_ack"},
            )
        return DecisionResult(
            outcome=DecisionOutcome.RESPOND,
            reason="direct mention",
            debug={"trigger": "mention"},
        )

    # 5. Reply to bot's message
    if inp.is_reply_to_bot:
        return DecisionResult(
            outcome=DecisionOutcome.RESPOND,
            reason="reply to bot's message",
            debug={"trigger": "reply_to_bot"},
        )

    # 6. Addressed to bot by name
    if inp.addressed_to_bot_by_name:
        return DecisionResult(
            outcome=DecisionOutcome.RESPOND,
            reason="addressed to bot by name",
            debug={"trigger": "name_address"},
        )

    # 7. Autonomy OFF
    if not inp.channel_autonomy_enabled:
        return DecisionResult(
            outcome=DecisionOutcome.IGNORE,
            reason="channel autonomy disabled — bot only replies when addressed",
        )

    # 8. Autonomy ON — only chime in for questions when bot was recently part of the convo
    if inp.is_question and inp.bot_spoke_recently:
        return DecisionResult(
            outcome=DecisionOutcome.RESPOND,
            reason="autonomous participation: question in active conversation",
            debug={"trigger": "autonomous_question"},
        )

    # 9. Otherwise
    return DecisionResult(
        outcome=DecisionOutcome.IGNORE,
        reason="no trigger to respond — staying silent",
    )


# ── LLM-assisted decision (Phase 2, behind flag) ──────────────
LLM_ASSIST_PROMPT = """\
You are deciding whether an AI groupmate bot should reply to a Discord message.

The bot was NOT directly mentioned. The bot was recently part of the conversation.
You must decide: should the bot reply (RESPOND), react with an emoji (REACT),
or stay silent (IGNORE)?

Consider:
- Does the bot have something USEFUL to add? (Not just a "yeah!")
- Would chiming in feel natural, or intrusive?
- Is the message a question someone else might answer?
- Is the bot being indirectly referenced?

Output JSON: {"decision": "RESPOND" | "REACT" | "IGNORE", "reason": "short reason"}

Default to IGNORE if unsure. Silence is better than awkward interruption.
"""


async def llm_assisted_decide(
    inp: DecisionInput,
    message_text: str,
    recent_context_summary: str = "",
) -> DecisionResult:
    """Ask the LLM whether to respond in a gray-zone autonomous case.

    Only called when:
    - enable_llm_decision_assist is True
    - Channel autonomy is enabled
    - Bot was recently speaking
    - It's not a direct mention or reply
    - The message is NOT a clear question (those already auto-respond)

    Returns a DecisionResult. On any error, defaults to IGNORE (silent).
    """
    from app.ai.provider import ProviderError, get_provider
    from app.ai.schemas import LLMMessage, LLMRequest

    user_prompt = f"""Recent context:
{recent_context_summary or '(none)'}

Current message: {message_text}

Decide: RESPOND, REACT, or IGNORE?"""

    request = LLMRequest(
        system_prompt=LLM_ASSIST_PROMPT,
        messages=[LLMMessage(role="user", content=user_prompt)],
        max_tokens=80,
        temperature=0.2,
        tools=[],
    )

    try:
        response = await get_provider().generate(request)
    except ProviderError as e:
        log.warning("llm_assist_failed", error=str(e))
        return DecisionResult(
            outcome=DecisionOutcome.IGNORE,
            reason="LLM assist failed — defaulting to silent",
        )

    import json
    raw = (response.content or "").strip()
    # Strip ```json fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines)

    try:
        parsed = json.loads(raw)
        decision_str = str(parsed.get("decision", "IGNORE")).upper()
        reason = str(parsed.get("reason", "LLM-assisted decision"))
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("llm_assist_parse_failed", error=str(e), raw_preview=raw[:200])
        return DecisionResult(
            outcome=DecisionOutcome.IGNORE,
            reason="LLM assist returned unparseable output — staying silent",
        )

    outcome_map = {
        "RESPOND": DecisionOutcome.RESPOND,
        "REACT": DecisionOutcome.REACT,
        "IGNORE": DecisionOutcome.IGNORE,
    }
    outcome = outcome_map.get(decision_str, DecisionOutcome.IGNORE)
    return DecisionResult(
        outcome=outcome,
        reason=f"LLM-assisted: {reason}",
        debug={"trigger": "llm_assist", "raw_decision": decision_str},
    )
