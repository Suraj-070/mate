"""Per-channel rolling conversation buffer.

Holds the last N messages per channel in memory for fast LLM context
assembly. Capped size; older messages fall off automatically.

This is NOT the database. The DB holds the full message log for audit;
the buffer holds only the working window for LLM context.

On bot restart, the buffer is empty. The context builder can fetch recent
messages from the DB to rebuild it lazily — see warm_from_db().
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from app.config.settings import get_settings


@dataclass
class BufferedMessage:
    """A single message in the conversation buffer."""

    discord_message_id: str
    author_id: str           # Discord user ID (snowflake as string)
    author_name: str         # Display name for prompt readability
    content: str
    is_bot: bool
    timestamp: float = field(default_factory=time.time)
    reply_to: Optional[str] = None  # discord_message_id of parent

    def to_llm_role(self) -> str:
        """Map to OpenAI chat role. Bot → assistant; everyone else → user."""
        return "assistant" if self.is_bot else "user"


class ConversationBuffer:
    """In-memory per-channel rolling buffer.

    Thread-safe for asyncio single-loop use (no lock needed — discord.py
    processes events serially per-event, and we're all in one event loop).
    """

    def __init__(self, max_size: int | None = None) -> None:
        self._buffers: dict[str, Deque[BufferedMessage]] = {}
        self._max_size = max_size or get_settings().context_buffer_size

    def append(self, channel_id: str, msg: BufferedMessage) -> None:
        buf = self._buffers.setdefault(channel_id, deque(maxlen=self._max_size))
        buf.append(msg)

    def get(self, channel_id: str, limit: int | None = None) -> list[BufferedMessage]:
        """Return last N messages in chronological order (oldest first)."""
        buf = self._buffers.get(channel_id)
        if not buf:
            return []
        items = list(buf)
        if limit is not None and limit < len(items):
            items = items[-limit:]
        return items

    def get_recent_bot_messages_count(
        self, channel_id: str, window_seconds: float = 300.0
    ) -> int:
        """How many bot messages in the last `window_seconds` — for rate limiting."""
        buf = self._buffers.get(channel_id)
        if not buf:
            return 0
        cutoff = time.time() - window_seconds
        return sum(1 for m in buf if m.is_bot and m.timestamp >= cutoff)

    def bot_spoke_in_last(
        self, channel_id: str, n_messages: int = 5, seconds: float = 300.0
    ) -> bool:
        """Did the bot speak in the last N messages or last `seconds`?"""
        buf = self._buffers.get(channel_id)
        if not buf:
            return False
        recent = list(buf)[-n_messages:]
        cutoff = time.time() - seconds
        return any(m.is_bot and m.timestamp >= cutoff for m in recent)

    def last_bot_message_time(self, channel_id: str) -> float:
        """Unix timestamp of bot's last message in channel, or 0.0 if none."""
        buf = self._buffers.get(channel_id)
        if not buf:
            return 0.0
        for m in reversed(buf):
            if m.is_bot:
                return m.timestamp
        return 0.0

    def clear(self, channel_id: str) -> None:
        self._buffers.pop(channel_id, None)

    def warm_from_db(self, channel_id: str, messages: list[BufferedMessage]) -> None:
        """Replace the buffer for a channel with the given messages (chronological)."""
        self._buffers[channel_id] = deque(messages, maxlen=self._max_size)


# Singleton — single buffer manager for the whole app
_buffer: ConversationBuffer | None = None


def get_buffer() -> ConversationBuffer:
    global _buffer
    if _buffer is None:
        _buffer = ConversationBuffer()
    return _buffer
