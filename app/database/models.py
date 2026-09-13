"""SQLAlchemy declarative models.

Phase 1: channels, users, messages.
Phase 2: conversation_summaries, memories, style_samples, style_profiles.
Phase 3: reminders, timers.
Phase 4: adds preferred_language column to users (migration 0004).

All BigInt IDs use `BigInteger().with_variant(Integer(), "sqlite")` so the
same model works on both SQLite (Integer AUTOINCREMENT) and Postgres (BigInt).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base. Use `Base.metadata` for Alembic autogenerate target."""


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    discord_guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_channel_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    def __repr__(self) -> str:
        return f"<Channel #{self.name} ({self.discord_channel_id})>"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    opt_out_style: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    opt_out_memory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    opt_out_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    preferred_language: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        doc="NULL=auto-detect per message. One of: en, ne-deva, ne-roman, mixed",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    def __repr__(self) -> str:
        return f"<User {self.username or self.discord_user_id}>"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=False
    )
    discord_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    reply_to_message_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    language_hint: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    def __repr__(self) -> str:
        return f"<Message {self.discord_message_id} ch={self.channel_id} bot={self.is_bot}>"


# ─────────────────────────────────────────────────────────────
# Phase 2 models
# ─────────────────────────────────────────────────────────────


class ConversationSummary(Base):
    """Compressed summary of older conversation messages per channel.

    One row per channel per rolling window. The context builder picks the
    most recent summary per channel and injects it into the prompt.

    Lifecycle: created by the summarizer background task when the channel's
    unsummarized message count crosses `summary_trigger_message_count`.
    Older summaries are soft-deleted (kept for audit) when superseded.
    """

    __tablename__ = "conversation_summaries"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=False
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Discord snowflakes — opaque strings, no FK to messages (messages may be GC'd)
    message_range_start: Mapped[str] = mapped_column(String(32), nullable=False)
    message_range_end: Mapped[str] = mapped_column(String(32), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
        doc="False once superseded by a newer summary",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_summaries_channel_active", "channel_id", "is_active"),
    )


class Memory(Base):
    """Long-term structured memory. Admin-curated only.

    Created via `/remember` slash command or context menu "Remember this".
    Source-tagged for audit. Confidence-scored. Expirable. Soft-deleted.

    The LLM may PROPOSE memories (via extraction), but never writes directly.
    Proposals are surfaced to admins for approval before becoming rows.
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    channel_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=True,
        doc="NULL = global memory; otherwise channel-scoped",
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True,
        doc="NULL = group-level memory; otherwise user-specific (never revealed to others)",
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False,
        doc="fact | preference | lore | event | joke",
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        doc="Discord snowflake of the message this memory came from, for audit",
    )
    created_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
        doc="Admin who approved this memory",
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0",
        doc="0.0–1.0. Admin-stated facts = 1.0; jokes = 0.3",
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        doc="NULL = no expiry; otherwise purged after this time",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_memories_channel_kind", "channel_id", "kind"),
    )


class StyleSample(Base):
    """Admin-approved message sample used for style learning.

    Source = a row in the messages table. Admin approves via context menu
    "Use for Style". The raw message content is NOT directly injected into
    LLM prompts — the style profile summarizer distills it into structured
    observations first.
    """

    __tablename__ = "style_samples"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=False
    )
    source_message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=False,
        doc="FK to messages.id (not Discord snowflake)",
    )
    approved_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    tags: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
        doc="JSON array of qualitative tags like ['casual', 'short', 'mixed_language']",
    )
    is_used_in_profile: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
        doc="Set True once a profile rebuild has consumed this sample",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_style_samples_channel", "channel_id"),
    )


class StyleProfile(Base):
    """Derived style profile per channel.

    One row per channel. Updated by the summarizer when new samples are
    approved. The profile_json contains structured fields (formality, humor,
    etc.) plus a small number of distilled example strings — never raw
    message content from individual users.
    """

    __tablename__ = "style_profiles"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=False, unique=True
    )
    profile_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_sample_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True,
        doc="Highest style_sample.id included in this profile, for incremental updates",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


# ─────────────────────────────────────────────────────────────
# Phase 3 models
# ─────────────────────────────────────────────────────────────


class Reminder(Base):
    """Persisted reminder. Fired by APScheduler at trigger_at (UTC).

    One-time reminders: recurrence is NULL, is_fired flips True after firing.
    Recurring reminders: recurrence is one of {'daily', 'weekly', 'weekdays',
    'weekends'} or a cron expression. is_fired stays False; the scheduler
    advances trigger_at after each fire.

    On bot restart, scheduler reads all rows where is_fired=False AND
    is_cancelled=False AND trigger_at IS IN THE FUTURE, and re-schedules them.
    Past-due rows are fired immediately (with misfire grace).
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
        doc="User who created the reminder",
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=False,
        doc="Channel to fire the reminder in",
    )
    trigger_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        doc="UTC datetime when the reminder fires",
    )
    recurrence: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        doc="NULL=one-time; 'daily'|'weekly'|'weekdays'|'weekends' or cron expr",
    )
    message: Mapped[str] = mapped_column(
        Text, nullable=False,
        doc="Reminder message to send",
    )
    is_fired: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    is_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_reminders_pending", "trigger_at"),
    )


class Timer(Base):
    """Short-duration in-channel timer. Fired by APScheduler at expires_at.

    Different from Reminder in intent:
    - Timers are short (minutes/hours, not days)
    - Timers don't have a freeform message — they just say "timer X done"
    - Timers always one-shot (no recurrence)
    - Both share the same scheduler infrastructure for simplicity

    Persisted so restart doesn't lose running timers.
    """

    __tablename__ = "timers"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channels.id"), nullable=False,
    )
    duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False,
        doc="Original duration for display purposes",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        doc="UTC datetime when timer fires",
    )
    label: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        doc="Optional label shown when timer fires",
    )
    is_fired: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    is_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_timers_pending", "expires_at"),
    )
