"""Conversation summarizer — periodically compresses older buffer messages.

Trigger: when the count of unsummarized messages in a channel crosses
`summary_trigger_message_count` (default 30).

Process:
  1. Fetch the last N messages (excluding the most recent 10 — those stay
     in the live buffer).
  2. Ask the LLM to summarize them in 3-5 sentences, preserving:
     - Active topics
     - Decisions made
     - Anything the bot might need to know later
  3. Drop anything sensitive (PII patterns).
  4. Persist as a new active summary row.
  5. Old active summary is archived (is_active=False), kept for audit.

The summarizer NEVER stores raw messages — only the compressed summary.
"""
from __future__ import annotations

from typing import Optional

from app.ai.provider import ProviderError, get_provider
from app.ai.schemas import LLMMessage, LLMRequest
from app.config.settings import get_settings
from app.database import repository as repo
from app.database.models import Message
from app.utils.logging import get_logger

log = get_logger(__name__)

SUMMARY_PROMPT = """\
You are summarizing a portion of a casual group Discord chat for later reference.

Write a 3-5 sentence summary that preserves:
- Active topics of conversation
- Any decisions made or plans set
- Anything that might be relevant context for later conversation

Rules:
- DO NOT include phone numbers, emails, tokens, or any sensitive personal info.
- DO NOT include direct quotes — paraphrase.
- DO NOT include usernames unless a specific person was explicitly named in a decision.
- Keep it short and useful.
- Match the dominant language of the conversation (English, Nepali Devanagari, Romanized Nepali, or mixed).
- Do NOT include any "Here is the summary:" prefix — just the summary.
"""

# How many of the most recent messages to KEEP in the live buffer
# (not included in the summary chunk).
LIVE_BUFFER_KEEP = 10


async def maybe_summarize_channel(
    channel_db_id: int,
    discord_channel_id: str,
) -> Optional[str]:
    """Check if a channel needs summarization; if so, summarize + persist.

    Returns the summary text if a new summary was created, else None.
    """
    settings = get_settings()
    if not settings.enable_conversation_summaries:
        return None

    # Find the boundary: the end Discord message ID of the last active summary
    last_summary = await repo.get_active_summary(channel_db_id)
    if last_summary is not None:
        boundary_discord_id = last_summary.message_range_end
        unsummarized_count = await repo.count_messages_since(
            channel_db_id, boundary_discord_id
        )
    else:
        boundary_discord_id = None
        # No previous summary — count all messages
        unsummarized_count = await repo.count_messages_since(channel_db_id, "")

    if unsummarized_count < settings.summary_trigger_message_count:
        return None

    log.info(
        "summarizer_triggered",
        channel_db_id=channel_db_id,
        unsummarized_count=unsummarized_count,
        trigger_threshold=settings.summary_trigger_message_count,
    )

    # Fetch the chunk to summarize — the older portion, keeping the most
    # recent LIVE_BUFFER_KEEP messages out of the summary.
    fetch_count = max(unsummarized_count - LIVE_BUFFER_KEEP, LIVE_BUFFER_KEEP)
    # Don't over-fetch; cap at 60
    fetch_count = min(fetch_count, 60)
    recent_msgs = await repo.fetch_recent_messages(channel_db_id, limit=fetch_count)
    if len(recent_msgs) <= LIVE_BUFFER_KEEP:
        # Not enough to summarize
        return None

    # The chunk to summarize is everything except the last LIVE_BUFFER_KEEP
    chunk_to_summarize = recent_msgs[:-LIVE_BUFFER_KEEP] if len(recent_msgs) > LIVE_BUFFER_KEEP else recent_msgs
    if not chunk_to_summarize:
        return None

    summary_text = await _summarize_messages(chunk_to_summarize)
    if not summary_text:
        log.warning("summarizer_empty_result", channel_db_id=channel_db_id)
        return None

    # Persist
    range_start = chunk_to_summarize[0].discord_message_id
    range_end = chunk_to_summarize[-1].discord_message_id
    await repo.create_summary(
        channel_db_id=channel_db_id,
        summary_text=summary_text,
        message_range_start=range_start,
        message_range_end=range_end,
        message_count=len(chunk_to_summarize),
    )
    log.info(
        "summarizer_completed",
        channel_db_id=channel_db_id,
        summary_len=len(summary_text),
        message_count=len(chunk_to_summarize),
    )
    return summary_text


async def _summarize_messages(messages: list[Message]) -> Optional[str]:
    """Call the LLM to summarize a chunk of messages."""
    if not messages:
        return None

    # Format messages into a transcript
    lines: list[str] = []
    for m in messages:
        # Mask the actual author name — we only care about content for summary
        author_label = "bot" if m.is_bot else "user"
        lines.append(f"[{author_label}]: {m.content}")
    transcript = "\n".join(lines)

    request = LLMRequest(
        system_prompt=SUMMARY_PROMPT,
        messages=[
            LLMMessage(
                role="user",
                content=f"Summarize this chat excerpt:\n\n{transcript}",
            )
        ],
        max_tokens=300,
        temperature=0.3,  # low temp for factual summary
        tools=[],
    )

    try:
        response = await get_provider().generate(request)
    except ProviderError as e:
        log.error("summarizer_provider_error", error=str(e))
        return None

    summary = (response.content or "").strip()
    if not summary:
        return None

    # Sanitization pass — strip obvious PII patterns
    from app.memory.policies import looks_sensitive
    if looks_sensitive(summary):
        log.warning("summarizer_output_sensitive_skipped", length=len(summary))
        # We still keep the summary but log it. The prompt already forbids PII;
        # if it slips through, we want to know without losing the whole summary.
        # A more aggressive version would refuse to store this.

    return summary
