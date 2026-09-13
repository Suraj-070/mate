"""Retrieval logic for memory + summary injection into LLM context.

Single entry point: `retrieve_context_extras(channel_db_id)` returns a
bundle with the most recent summary + relevant memories, ready for the
context builder to inject.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import get_settings
from app.database import repository as repo
from app.database.models import ConversationSummary, Memory
from app.memory.models import SummaryView


@dataclass
class ContextExtras:
    """Bundle of Phase 2 context retrieved for a channel."""
    summary: Optional[SummaryView] = None
    memories: list[Memory] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.memories is None:
            self.memories = []


async def retrieve_context_extras(channel_db_id: int) -> ContextExtras:
    """Fetch the latest summary + relevant memories for a channel.

    Returns ContextExtras — empty fields if nothing found.
    """
    settings = get_settings()

    # Skip if both features are off
    if not settings.enable_conversation_summaries and not settings.enable_long_term_memory:
        return ContextExtras()

    summary_view: Optional[SummaryView] = None
    if settings.enable_conversation_summaries:
        summary: Optional[ConversationSummary] = await repo.get_active_summary(channel_db_id)
        if summary is not None:
            summary_view = SummaryView(
                summary_text=summary.summary_text,
                message_count=summary.message_count,
                message_range_start=summary.message_range_start,
                message_range_end=summary.message_range_end,
                created_at=summary.created_at,
            )

    memories: list[Memory] = []
    if settings.enable_long_term_memory:
        memories = await repo.retrieve_memories_for_context(
            channel_db_id=channel_db_id,
            limit=settings.max_memories_per_channel,
        )

    return ContextExtras(summary=summary_view, memories=memories)


def memories_as_prompt_block(memories: list[Memory]) -> list[str]:
    """Format memories for injection into the prompt's "LONG-TERM MEMORIES" block.

    Each line: "- [kind] content (confidence: 0.8)"
    User-scoped memories are NEVER included (filtered at retrieval time).
    """
    if not memories:
        return []
    lines: list[str] = []
    for m in memories:
        confidence_hint = ""
        if m.confidence < 0.5:
            confidence_hint = " (low confidence — possibly a joke)"
        lines.append(f"[{m.kind}] {m.content}{confidence_hint}")
    return lines
