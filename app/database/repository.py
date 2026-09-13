"""Data-access functions for messages, channels, users, and Phase 2 tables.

Keeps DB I/O in one place so the bot layer never touches SQLAlchemy directly.
All functions are async and use session_scope() internally.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import session_scope
from app.database.models import (
    Channel,
    ConversationSummary,
    Memory,
    Message,
    Reminder,
    StyleProfile,
    StyleSample,
    Timer,
    User,
)
from app.utils.logging import get_logger
from app.utils.text import detect_script

log = get_logger(__name__)


# ── Channels ──────────────────────────────────────────────────
async def get_or_create_channel(
    guild_id: str,
    channel_id: str,
    name: str,
    enabled: bool = True,
) -> Channel:
    async with session_scope() as session:
        result = await session.execute(
            select(Channel).where(Channel.discord_channel_id == channel_id)
        )
        channel = result.scalar_one_or_none()
        if channel is None:
            channel = Channel(
                discord_guild_id=guild_id,
                discord_channel_id=channel_id,
                name=name,
                enabled=enabled,
            )
            session.add(channel)
            await session.flush()
            log.info("channel_created", channel_id=channel_id, name=name)
        else:
            # Update name if changed (Discord channels can be renamed)
            if channel.name != name:
                channel.name = name
            if channel.enabled != enabled and enabled is True:
                # Don't auto-disable, only auto-enable on first see
                channel.enabled = enabled
        return channel


async def set_channel_enabled(channel_id: str, enabled: bool) -> None:
    async with session_scope() as session:
        result = await session.execute(
            select(Channel).where(Channel.discord_channel_id == channel_id)
        )
        channel = result.scalar_one_or_none()
        if channel:
            channel.enabled = enabled
            log.info("channel_toggled", channel_id=channel_id, enabled=enabled)


async def is_channel_enabled(channel_id: str) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            select(Channel.enabled).where(Channel.discord_channel_id == channel_id)
        )
        row = result.first()
        # Unknown channels: default enabled (first-seen behavior).
        # Known channels: respect stored flag.
        return bool(row[0]) if row else True


# ── Users ─────────────────────────────────────────────────────
async def get_or_create_user(
    discord_user_id: str,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    is_admin: bool = False,
) -> User:
    async with session_scope() as session:
        result = await session.execute(
            select(User).where(User.discord_user_id == discord_user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                discord_user_id=discord_user_id,
                username=username,
                display_name=display_name,
                is_admin=is_admin,
            )
            session.add(user)
            await session.flush()
            log.info("user_created", discord_user_id=discord_user_id, username=username)
        else:
            # Refresh cached info
            if username and user.username != username:
                user.username = username
            if display_name and user.display_name != display_name:
                user.display_name = display_name
        return user


async def get_user_by_discord_id(discord_user_id: str) -> Optional[User]:
    async with session_scope() as session:
        result = await session.execute(
            select(User).where(User.discord_user_id == discord_user_id)
        )
        return result.scalar_one_or_none()


async def get_user_by_id(user_db_id: int) -> Optional[User]:
    """Look up a user by their DB primary key."""
    async with session_scope() as session:
        result = await session.execute(
            select(User).where(User.id == user_db_id)
        )
        return result.scalar_one_or_none()


async def get_or_create_bot_user(discord_user_id: str, display_name: str = "Bot") -> User:
    """Get or create the bot's own user row. Used for FK on bot message logs.

    The bot is NOT an admin (it never issues admin commands). Its row exists
    purely so the messages table FK constraint is satisfied for bot replies.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(User).where(User.discord_user_id == discord_user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                discord_user_id=discord_user_id,
                username=display_name.lower().replace(" ", "_"),
                display_name=display_name,
                is_admin=False,
                opt_out_style=True,   # bot's own messages never used for style learning
                opt_out_memory=True,
                opt_out_reply=True,
            )
            session.add(user)
            await session.flush()
            log.info("bot_user_created", discord_user_id=discord_user_id)
        else:
            if display_name and user.display_name != display_name:
                user.display_name = display_name
        return user


# ── Messages ─────────────────────────────────────────────────
async def log_message(
    channel_db_id: int,
    author_db_id: int,
    discord_message_id: str,
    content: str,
    is_bot: bool,
    reply_to_message_id: Optional[str] = None,
) -> Message:
    """Persist a message row for audit + buffer rebuild + style sampling source.

    NEVER used to dump into LLM context directly — the in-memory
    conversation_buffer handles that.
    """
    script = detect_script(content)
    language_hint = {
        "latin": "en",         # heuristic — could be Romanized Nepali
        "devanagari": "ne",
        "mixed": "mixed",
        "other": "other",
    }.get(script.value, None)

    async with session_scope() as session:
        msg = Message(
            channel_id=channel_db_id,
            discord_message_id=discord_message_id,
            author_id=author_db_id,
            content=content,
            is_bot=is_bot,
            reply_to_message_id=reply_to_message_id,
            language_hint=language_hint,
        )
        session.add(msg)
        await session.flush()
        return msg


async def fetch_recent_messages(channel_db_id: int, limit: int = 20) -> list[Message]:
    """Fetch the most recent N messages for a channel (for buffer rebuild on restart)."""
    async with session_scope() as session:
        result = await session.execute(
            select(Message)
            .where(Message.channel_id == channel_db_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        rows.reverse()  # chronological order
        return rows


async def fetch_message_by_discord_id(
    channel_db_id: int, discord_message_id: str
) -> Optional[Message]:
    """Look up a stored message row by its Discord snowflake.

    Used by the style-sample approval flow (right-click → "Use for Style")
    to find the source message.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(Message).where(
                Message.channel_id == channel_db_id,
                Message.discord_message_id == discord_message_id,
            )
        )
        return result.scalar_one_or_none()


async def count_messages_since(channel_db_id: int, since_discord_id: str) -> int:
    """Count messages in a channel since a given Discord message ID (exclusive).

    Used by the summarizer to know when to trigger the next summary.
    Approximation: counts messages created at or after the timestamp of the
    `since` message. Snowflake ordering is preserved via created_at.
    """
    async with session_scope() as session:
        # First find the timestamp of the boundary message
        boundary_result = await session.execute(
            select(Message.created_at).where(
                Message.channel_id == channel_db_id,
                Message.discord_message_id == since_discord_id,
            )
        )
        boundary_row = boundary_result.first()
        if boundary_row is None:
            # No boundary — count all messages in channel
            result = await session.execute(
                select(func.count(Message.id)).where(Message.channel_id == channel_db_id)
            )
            return int(result.scalar() or 0)
        boundary_ts = boundary_row[0]
        result = await session.execute(
            select(func.count(Message.id)).where(
                Message.channel_id == channel_db_id,
                Message.created_at > boundary_ts,
            )
        )
        return int(result.scalar() or 0)


# ═══════════════════════════════════════════════════════════════
# Phase 2: Conversation summaries
# ═══════════════════════════════════════════════════════════════
async def get_active_summary(channel_db_id: int) -> Optional[ConversationSummary]:
    """Get the most recent active summary for a channel."""
    async with session_scope() as session:
        result = await session.execute(
            select(ConversationSummary)
            .where(
                ConversationSummary.channel_id == channel_db_id,
                ConversationSummary.is_active == True,  # noqa: E712
            )
            .order_by(ConversationSummary.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def create_summary(
    channel_db_id: int,
    summary_text: str,
    message_range_start: str,
    message_range_end: str,
    message_count: int,
) -> ConversationSummary:
    """Create a new summary and mark any previous active summary as inactive.

    Old summaries are soft-archived (is_active=False), NOT deleted — they
    remain in the table for audit.
    """
    async with session_scope() as session:
        # Deactivate previous active summary for this channel
        await session.execute(
            update(ConversationSummary)
            .where(
                ConversationSummary.channel_id == channel_db_id,
                ConversationSummary.is_active == True,  # noqa: E712
            )
            .values(is_active=False)
        )
        summary = ConversationSummary(
            channel_id=channel_db_id,
            summary_text=summary_text,
            message_range_start=message_range_start,
            message_range_end=message_range_end,
            message_count=message_count,
            is_active=True,
        )
        session.add(summary)
        await session.flush()
        log.info(
            "summary_created",
            channel_db_id=channel_db_id,
            message_count=message_count,
            text_len=len(summary_text),
        )
        return summary


# ═══════════════════════════════════════════════════════════════
# Phase 2: Long-term memory
# ═══════════════════════════════════════════════════════════════
async def create_memory(
    channel_db_id: Optional[int],
    user_db_id: Optional[int],
    kind: str,
    content: str,
    created_by_user_id: int,
    source_message_id: Optional[str] = None,
    confidence: float = 1.0,
    expires_at: Optional[datetime] = None,
) -> Memory:
    """Persist a new admin-approved memory row.

    Permission check (is_admin) must be done by the caller BEFORE calling this.
    """
    async with session_scope() as session:
        mem = Memory(
            channel_id=channel_db_id,
            user_id=user_db_id,
            kind=kind,
            content=content,
            source_message_id=source_message_id,
            created_by_user_id=created_by_user_id,
            confidence=confidence,
            expires_at=expires_at,
            is_deleted=False,
        )
        session.add(mem)
        await session.flush()
        log.info(
            "memory_created",
            memory_id=mem.id,
            channel_db_id=channel_db_id,
            kind=kind,
            user_db_id=user_db_id,
        )
        return mem


async def get_memory(memory_id: int) -> Optional[Memory]:
    async with session_scope() as session:
        result = await session.execute(
            select(Memory).where(
                Memory.id == memory_id,
                Memory.is_deleted == False,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()


async def list_memories(
    channel_db_id: Optional[int] = None,
    include_user_scoped: bool = False,
    limit: int = 50,
) -> list[Memory]:
    """List non-deleted memories, optionally filtered by channel.

    By default excludes user-scoped memories (those with user_id set) — only
    group-level memories are listed in `/memories list`. This protects user
    privacy by never surfacing user-specific memories to the whole group.
    """
    async with session_scope() as session:
        stmt = select(Memory).where(Memory.is_deleted == False)  # noqa: E712
        if channel_db_id is not None:
            # Channel-scoped OR global (channel_id IS NULL)
            stmt = stmt.where(
                (Memory.channel_id == channel_db_id) | (Memory.channel_id.is_(None))
            )
        if not include_user_scoped:
            stmt = stmt.where(Memory.user_id.is_(None))
        stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def retrieve_memories_for_context(
    channel_db_id: int,
    limit: int = 10,
) -> list[Memory]:
    """Retrieve memories relevant to a channel for LLM context injection.

    Returns both channel-scoped and global memories. Excludes:
    - Deleted memories
    - Expired memories (expiry_at < now)
    - User-scoped memories (NEVER injected into shared context — only the
      owning user should ever see those, and that's a Phase 3+ feature)

    Caller is responsible for filtering by `kind` if needed.
    """
    async with session_scope() as session:
        now = datetime.now(timezone.utc)
        stmt = (
            select(Memory)
            .where(
                Memory.is_deleted == False,  # noqa: E712
                Memory.user_id.is_(None),  # privacy: never inject user-scoped memories
                (Memory.expires_at.is_(None)) | (Memory.expires_at > now),
                (Memory.channel_id == channel_db_id) | (Memory.channel_id.is_(None)),
            )
            .order_by(Memory.confidence.desc(), Memory.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def delete_memory(memory_id: int) -> bool:
    """Soft-delete a memory by ID. Returns True if a row was updated."""
    async with session_scope() as session:
        result = await session.execute(
            update(Memory)
            .where(
                Memory.id == memory_id,
                Memory.is_deleted == False,  # noqa: E712
            )
            .values(is_deleted=True, updated_at=datetime.now(timezone.utc))
        )
        deleted = result.rowcount or 0
        if deleted:
            log.info("memory_deleted", memory_id=memory_id)
        return deleted > 0


async def sweep_expired_memories() -> int:
    """Mark expired memories as deleted. Returns count swept.

    Called periodically by the background sweeper.
    """
    async with session_scope() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            update(Memory)
            .where(
                Memory.is_deleted == False,  # noqa: E712
                Memory.expires_at.is_not(None),
                Memory.expires_at <= now,
            )
            .values(is_deleted=True, updated_at=now)
        )
        count = result.rowcount or 0
        if count:
            log.info("expired_memories_swept", count=count)
        return count


# ═══════════════════════════════════════════════════════════════
# Phase 2: Style samples + profile
# ═══════════════════════════════════════════════════════════════
async def add_style_sample(
    channel_db_id: int,
    source_message_db_id: int,
    approved_by_user_id: int,
    tags: Optional[list[str]] = None,
) -> StyleSample:
    """Record admin approval of a message as a style sample.

    Idempotent: if the same (channel, source_message) pair already exists,
    returns the existing row.
    """
    import json
    async with session_scope() as session:
        # Check for existing
        existing = await session.execute(
            select(StyleSample).where(
                StyleSample.channel_id == channel_db_id,
                StyleSample.source_message_id == source_message_db_id,
            )
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row is not None:
            log.info("style_sample_already_exists", sample_id=existing_row.id)
            return existing_row

        sample = StyleSample(
            channel_id=channel_db_id,
            source_message_id=source_message_db_id,
            approved_by_user_id=approved_by_user_id,
            tags=json.dumps(tags or []),
            is_used_in_profile=False,
        )
        session.add(sample)
        await session.flush()
        log.info(
            "style_sample_added",
            sample_id=sample.id,
            channel_db_id=channel_db_id,
            source_message_db_id=source_message_db_id,
        )
        return sample


async def list_style_samples(channel_db_id: int, limit: int = 50) -> list[tuple[StyleSample, Message]]:
    """List recent style samples for a channel, joined with their source messages.

    Returns list of (sample, message) tuples.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(StyleSample, Message)
            .join(Message, StyleSample.source_message_id == Message.id)
            .where(StyleSample.channel_id == channel_db_id)
            .order_by(StyleSample.created_at.desc())
            .limit(limit)
        )
        return list(result.all())


async def count_unused_style_samples(channel_db_id: int) -> int:
    """Count style samples that haven't been folded into the profile yet."""
    async with session_scope() as session:
        result = await session.execute(
            select(func.count(StyleSample.id)).where(
                StyleSample.channel_id == channel_db_id,
                StyleSample.is_used_in_profile == False,  # noqa: E712
            )
        )
        return int(result.scalar() or 0)


async def fetch_style_sample_contents(channel_db_id: int, limit: int = 30) -> list[str]:
    """Fetch the raw contents of approved samples for profile rebuild.

    Returns up to `limit` message contents from samples for this channel,
    preferring ones not yet used in the profile, then older used ones as fallback.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(Message.content)
            .join(StyleSample, StyleSample.source_message_id == Message.id)
            .where(StyleSample.channel_id == channel_db_id)
            .order_by(
                StyleSample.is_used_in_profile.asc(),  # unused first
                StyleSample.created_at.desc(),
            )
            .limit(limit)
        )
        return [row[0] for row in result.all()]


async def mark_style_samples_used(channel_db_id: int, up_to_sample_id: int) -> None:
    """Mark all samples in this channel up to (and including) the given ID as used."""
    async with session_scope() as session:
        await session.execute(
            update(StyleSample)
            .where(
                StyleSample.channel_id == channel_db_id,
                StyleSample.id <= up_to_sample_id,
                StyleSample.is_used_in_profile == False,  # noqa: E712
            )
            .values(is_used_in_profile=True)
        )


async def get_style_profile(channel_db_id: int) -> Optional[StyleProfile]:
    async with session_scope() as session:
        result = await session.execute(
            select(StyleProfile).where(StyleProfile.channel_id == channel_db_id)
        )
        return result.scalar_one_or_none()


async def upsert_style_profile(
    channel_db_id: int,
    profile_json: str,
    sample_count: int,
    last_sample_id: Optional[int] = None,
) -> StyleProfile:
    """Insert or update the style profile row for a channel."""
    import json
    async with session_scope() as session:
        existing = await session.execute(
            select(StyleProfile).where(StyleProfile.channel_id == channel_db_id)
        )
        profile = existing.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if profile is None:
            profile = StyleProfile(
                channel_id=channel_db_id,
                profile_json=profile_json,
                sample_count=sample_count,
                last_sample_id=last_sample_id,
                updated_at=now,
            )
            session.add(profile)
        else:
            profile.profile_json = profile_json
            profile.sample_count = sample_count
            profile.last_sample_id = last_sample_id
            profile.updated_at = now
        await session.flush()
        log.info(
            "style_profile_upserted",
            channel_db_id=channel_db_id,
            sample_count=sample_count,
        )
        return profile


# ═══════════════════════════════════════════════════════════════
# Phase 2: User privacy flags
# ═══════════════════════════════════════════════════════════════
async def set_user_opt_out(
    discord_user_id: str,
    flag: str,
    value: bool,
) -> bool:
    """Set one of the opt_out_* flags on a user.

    flag must be one of: 'style' | 'memory' | 'reply'.
    Returns True if the user was found and updated.
    """
    if flag not in ("style", "memory", "reply"):
        raise ValueError(f"Invalid opt-out flag: {flag}")
    column_name = f"opt_out_{flag}"
    async with session_scope() as session:
        user = await session.execute(
            select(User).where(User.discord_user_id == discord_user_id)
        )
        user_obj = user.scalar_one_or_none()
        if user_obj is None:
            return False
        setattr(user_obj, column_name, value)
        user_obj.updated_at = datetime.now(timezone.utc)
        log.info(
            "user_opt_out_set",
            discord_user_id=discord_user_id,
            flag=flag,
            value=value,
        )
        return True


# ═══════════════════════════════════════════════════════════════
# Phase 4: Per-user language preference
# ═══════════════════════════════════════════════════════════════
VALID_LANGUAGES = {"en", "ne-deva", "ne-roman", "mixed"}


async def set_user_preferred_language(
    discord_user_id: str,
    language: Optional[str],
) -> bool:
    """Set the preferred_language field on a user.

    Pass None to clear (auto-detect per message).
    Returns True if the user was found and updated.
    Raises ValueError if language is not in VALID_LANGUAGES.
    """
    if language is not None and language not in VALID_LANGUAGES:
        raise ValueError(
            f"Invalid language '{language}'. Must be one of: {sorted(VALID_LANGUAGES)}"
        )

    async with session_scope() as session:
        user = await session.execute(
            select(User).where(User.discord_user_id == discord_user_id)
        )
        user_obj = user.scalar_one_or_none()
        if user_obj is None:
            return False
        user_obj.preferred_language = language
        user_obj.updated_at = datetime.now(timezone.utc)
        log.info(
            "user_preferred_language_set",
            discord_user_id=discord_user_id,
            language=language,
        )
        return True


async def get_user_preferred_language(discord_user_id: str) -> Optional[str]:
    """Returns the user's preferred language, or None if unset/auto-detect."""
    user = await get_user_by_discord_id(discord_user_id)
    if user is None:
        return None
    return user.preferred_language


# ═══════════════════════════════════════════════════════════════
# Phase 3: Reminders
# ═══════════════════════════════════════════════════════════════
async def create_reminder(
    user_db_id: int,
    channel_db_id: int,
    trigger_at: datetime,
    message: str,
    recurrence: Optional[str] = None,
) -> Reminder:
    """Persist a new reminder row."""
    async with session_scope() as session:
        reminder = Reminder(
            user_id=user_db_id,
            channel_id=channel_db_id,
            trigger_at=trigger_at,
            recurrence=recurrence,
            message=message,
            is_fired=False,
            is_cancelled=False,
        )
        session.add(reminder)
        await session.flush()
        log.info(
            "reminder_created",
            reminder_id=reminder.id,
            channel_db_id=channel_db_id,
            trigger_at=trigger_at.isoformat(),
            recurrence=recurrence,
        )
        return reminder


async def get_reminder(reminder_id: int) -> Optional[Reminder]:
    async with session_scope() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.id == reminder_id)
        )
        return result.scalar_one_or_none()


async def list_pending_reminders(
    channel_db_id: Optional[int] = None,
    user_db_id: Optional[int] = None,
    limit: int = 50,
) -> list[Reminder]:
    """List unfired + uncancelled reminders, optionally filtered."""
    async with session_scope() as session:
        stmt = (
            select(Reminder)
            .where(
                Reminder.is_fired == False,  # noqa: E712
                Reminder.is_cancelled == False,  # noqa: E712
            )
            .order_by(Reminder.trigger_at.asc())
            .limit(limit)
        )
        if channel_db_id is not None:
            stmt = stmt.where(Reminder.channel_id == channel_db_id)
        if user_db_id is not None:
            stmt = stmt.where(Reminder.user_id == user_db_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_all_pending_reminders_for_recovery() -> list[Reminder]:
    """Used by the scheduler on startup: all unfired + uncancelled rows."""
    async with session_scope() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.is_fired == False,  # noqa: E712
                Reminder.is_cancelled == False,  # noqa: E712
            )
        )
        return list(result.scalars().all())


async def mark_reminder_fired(reminder_id: int) -> bool:
    """Mark a reminder as fired. For one-time reminders, this effectively retires it."""
    async with session_scope() as session:
        result = await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(is_fired=True)
        )
        return (result.rowcount or 0) > 0


async def advance_recurring_reminder(reminder_id: int, next_trigger_at: datetime) -> None:
    """For recurring reminders: keep is_fired=False, advance trigger_at to next occurrence."""
    async with session_scope() as session:
        await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(trigger_at=next_trigger_at)
        )


async def cancel_reminder(reminder_id: int) -> bool:
    """Soft-cancel a reminder."""
    async with session_scope() as session:
        result = await session.execute(
            update(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.is_cancelled == False,  # noqa: E712
                Reminder.is_fired == False,  # noqa: E712
            )
            .values(is_cancelled=True)
        )
        cancelled = (result.rowcount or 0) > 0
        if cancelled:
            log.info("reminder_cancelled", reminder_id=reminder_id)
        return cancelled


# ═══════════════════════════════════════════════════════════════
# Phase 3: Timers
# ═══════════════════════════════════════════════════════════════
async def create_timer(
    user_db_id: int,
    channel_db_id: int,
    duration_seconds: int,
    expires_at: datetime,
    label: Optional[str] = None,
) -> Timer:
    """Persist a new timer row."""
    async with session_scope() as session:
        timer = Timer(
            user_id=user_db_id,
            channel_id=channel_db_id,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
            label=label,
            is_fired=False,
            is_cancelled=False,
        )
        session.add(timer)
        await session.flush()
        log.info(
            "timer_created",
            timer_id=timer.id,
            channel_db_id=channel_db_id,
            duration_seconds=duration_seconds,
            expires_at=expires_at.isoformat(),
        )
        return timer


async def get_timer(timer_id: int) -> Optional[Timer]:
    async with session_scope() as session:
        result = await session.execute(
            select(Timer).where(Timer.id == timer_id)
        )
        return result.scalar_one_or_none()


async def list_pending_timers(
    channel_db_id: Optional[int] = None,
    user_db_id: Optional[int] = None,
    limit: int = 50,
) -> list[Timer]:
    async with session_scope() as session:
        stmt = (
            select(Timer)
            .where(
                Timer.is_fired == False,  # noqa: E712
                Timer.is_cancelled == False,  # noqa: E712
            )
            .order_by(Timer.expires_at.asc())
            .limit(limit)
        )
        if channel_db_id is not None:
            stmt = stmt.where(Timer.channel_id == channel_db_id)
        if user_db_id is not None:
            stmt = stmt.where(Timer.user_id == user_db_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_all_pending_timers_for_recovery() -> list[Timer]:
    """Used by scheduler on startup."""
    async with session_scope() as session:
        result = await session.execute(
            select(Timer).where(
                Timer.is_fired == False,  # noqa: E712
                Timer.is_cancelled == False,  # noqa: E712
            )
        )
        return list(result.scalars().all())


async def mark_timer_fired(timer_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            update(Timer)
            .where(Timer.id == timer_id)
            .values(is_fired=True)
        )
        return (result.rowcount or 0) > 0


async def cancel_timer(timer_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            update(Timer)
            .where(
                Timer.id == timer_id,
                Timer.is_cancelled == False,  # noqa: E712
                Timer.is_fired == False,  # noqa: E712
            )
            .values(is_cancelled=True)
        )
        cancelled = (result.rowcount or 0) > 0
        if cancelled:
            log.info("timer_cancelled", timer_id=timer_id)
        return cancelled


# ── Helper: look up Channel by Discord channel ID ────────────
async def get_channel_by_discord_id(discord_channel_id: str) -> Optional[Channel]:
    async with session_scope() as session:
        result = await session.execute(
            select(Channel).where(Channel.discord_channel_id == discord_channel_id)
        )
        return result.scalar_one_or_none()


async def get_channel_db_id(discord_channel_id: str) -> Optional[int]:
    """Convenience: just the DB ID."""
    channel = await get_channel_by_discord_id(discord_channel_id)
    return channel.id if channel else None
