"""Pydantic schemas for the memory subsystem.

These are wire shapes between modules — NOT the DB models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MemoryKind(str, Enum):
    """The kind of memory. Affects retrieval priority + presentation."""
    FACT = "fact"                 # admin-stated fact about the group
    PREFERENCE = "preference"     # what the group likes/dislikes
    LORE = "lore"                 # inside jokes, group history
    EVENT = "event"               # recurring or upcoming events
    JOKE = "joke"                # recurring jokes (low confidence by default)


class MemoryCreate(BaseModel):
    """Inputs for creating a new memory via slash command."""
    kind: MemoryKind = MemoryKind.FACT
    content: str = Field(min_length=1, max_length=500)
    channel_scope: Optional[str] = Field(
        default=None,
        description="Discord channel ID to scope this memory to. None = global.",
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    expires_in_days: Optional[int] = Field(
        default=None,
        description="Optional expiry. None = never expires.",
    )


class MemoryView(BaseModel):
    """Public view of a memory (no internal fields like FK IDs)."""
    id: int
    kind: MemoryKind
    content: str
    channel_scope: Optional[str]
    confidence: float
    expires_at: Optional[datetime]
    created_at: datetime

    @classmethod
    def from_orm_model(cls, m, channel_id_lookup: dict[int, str] | None = None) -> "MemoryView":
        channel_scope = None
        if m.channel_id is not None and channel_id_lookup:
            channel_scope = channel_id_lookup.get(m.channel_id)
        return cls(
            id=m.id,
            kind=MemoryKind(m.kind),
            content=m.content,
            channel_scope=channel_scope,
            confidence=m.confidence,
            expires_at=m.expires_at,
            created_at=m.created_at,
        )


class SummaryView(BaseModel):
    """Public view of a conversation summary."""
    summary_text: str
    message_count: int
    message_range_start: str
    message_range_end: str
    created_at: datetime


class ExtractedMemoryProposal(BaseModel):
    """A memory proposal extracted by the LLM from recent chat.

    The LLM proposes; admins approve. Never auto-committed.
    """
    kind: MemoryKind
    content: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = Field(default="", description="Why the LLM thinks this is worth remembering")
