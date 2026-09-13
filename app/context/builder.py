"""Context builder — assembles the LLM request from all sources.

This is the only place that decides what goes into the LLM prompt.
If you ever need to change the context strategy (RAG, hierarchical
memory, compression), this is the file to rewrite.

Phase 2 additions:
- Fetches per-channel style profile from DB
- Fetches latest conversation summary
- Fetches relevant long-term memories
- All three are passed to build_system_prompt()

Token budget:
- system_prompt: ~600-1500 tokens (depending on style/examples)
- recent context: rest of the budget, capped by CONTEXT_MAX_TOKENS
- reserved for response: ~300 tokens

The builder NEVER exceeds the budget. It trims oldest messages first
until it fits.
"""
from __future__ import annotations

from app.ai.prompts import build_system_prompt
from app.ai.schemas import LLMMessage, LLMRequest
from app.config.settings import get_settings
from app.context.conversation_buffer import BufferedMessage, get_buffer
from app.context.topic_context import get_channel_topic, topic_as_prompt_block
from app.database import repository as repo
from app.memory.retrieval import (
    ContextExtras,
    memories_as_prompt_block,
    retrieve_context_extras,
)
from app.personality.base_personality import default_personality
from app.personality.style_profile import (
    StyleProfile,
    default_style_profile,
    load_style_profile_for_channel,
)
from app.utils.logging import get_logger
from app.utils.text import estimate_tokens

log = get_logger(__name__)


# Cache channel DB id → style profile to avoid DB hit on every message.
# Invalidated when a profile is rebuilt.
_style_profile_cache: dict[int, StyleProfile] = {}


def invalidate_style_cache(channel_db_id: int | None = None) -> None:
    """Invalidate cached style profile(s). Call after rebuild."""
    if channel_db_id is None:
        _style_profile_cache.clear()
    else:
        _style_profile_cache.pop(channel_db_id, None)


def _format_user_message(msg: BufferedMessage, is_current: bool = False) -> str:
    """Format a buffered message for the LLM.

    We prefix each user message with the author's display name so the LLM
    can attribute statements correctly in a multi-user channel. The
    current message is marked so the LLM knows it's the one to respond to.
    """
    prefix = "[now]" if is_current else ""
    return f"{prefix}[{msg.author_name}]: {msg.content}".strip()


async def build_request(
    channel_db_id: int,
    discord_channel_id: str,
    current_message: BufferedMessage,
    tool_definitions: list[dict] | None = None,
    tool_context: "ToolContext | None" = None,  # noqa: F821 — type-only, lazy import
    user_db_id: int | None = None,  # Phase 4: for fetching user language preference
) -> LLMRequest:
    """Assemble an LLMRequest ready to send to provider.

    Phase 2: also fetches summary, memories, and per-channel style profile.
    Phase 3: also injects tool schemas if tools are enabled + a context is provided.
    Phase 4: also injects active topic + per-user language preference.

    Args:
        channel_db_id: DB ID of the channel (not Discord ID)
        discord_channel_id: Discord channel ID (string)
        current_message: the message that triggered this request
        tool_definitions: Phase 3+ tool schemas (auto-loaded if None and tools enabled)
        tool_context: Phase 3+ context for permission checks (passed through to orchestrator)
        user_db_id: Phase 4 — DB ID of the user to fetch language preference for
    """
    settings = get_settings()
    buffer = get_buffer()

    # ── Phase 3: auto-load tool schemas if not explicitly provided ──
    if tool_definitions is None and settings.enable_tools:
        from app.tools.registry import get_default_registry
        try:
            registry = get_default_registry()
            tool_definitions = registry.all_schemas() if len(registry) > 0 else None
        except Exception as e:
            log.warning("tool_schema_load_failed", error=str(e))
            tool_definitions = None

    # ── Phase 2: load style profile (cached) ───────────────────
    if channel_db_id in _style_profile_cache:
        style = _style_profile_cache[channel_db_id]
    else:
        style = await load_style_profile_for_channel(channel_db_id)
        _style_profile_cache[channel_db_id] = style

    # ── Phase 2: retrieve summary + memories ──────────────────
    extras: ContextExtras = await retrieve_context_extras(channel_db_id)
    long_term_memories = memories_as_prompt_block(extras.memories)
    summary_text = extras.summary.summary_text if extras.summary else None

    # ── Phase 4: detect active topic ──────────────────────────
    topic_block: str | None = None
    if settings.enable_topic_tracking:
        topic = get_channel_topic(discord_channel_id)
        topic_block = topic_as_prompt_block(topic)

    # ── Phase 4: fetch user's language preference ─────────────
    user_language_hint: str | None = None
    if settings.enable_per_user_language and user_db_id is not None:
        try:
            from app.database.repository import get_user_by_id
            user = await get_user_by_id(user_db_id)
            if user is not None and user.preferred_language:
                user_language_hint = user.preferred_language
        except Exception as e:
            log.warning("user_language_lookup_failed", error=str(e), user_db_id=user_db_id)

    # Build the system prompt
    system_prompt = build_system_prompt(
        personality=default_personality(),
        style=style,
        tool_definitions=tool_definitions,
        long_term_memories=long_term_memories,
        conversation_summary=summary_text,
        topic_block=topic_block,
        user_language_hint=user_language_hint,
    )

    # ── Build the conversation window ────────────────────────
    all_msgs = buffer.get(discord_channel_id)

    llm_messages: list[LLMMessage] = []
    current_group: list[BufferedMessage] = []

    def flush_group(group: list[BufferedMessage]) -> None:
        if not group:
            return
        texts = [_format_user_message(m, is_current=(m is current_message)) for m in group]
        combined = "\n".join(texts)
        llm_messages.append(
            LLMMessage(
                role="user",
                content=combined,
                name=group[0].author_name.replace(" ", "_").lower()[:32] or "user",
            )
        )

    for msg in all_msgs:
        if msg.discord_message_id == current_message.discord_message_id:
            continue
        if msg.is_bot:
            flush_group(current_group)
            current_group = []
            llm_messages.append(LLMMessage(role="assistant", content=msg.content))
        else:
            if current_group and current_group[-1].author_id != msg.author_id:
                flush_group(current_group)
                current_group = []
            current_group.append(msg)
    flush_group(current_group)

    # Append the current message as the final user turn (always)
    llm_messages.append(
        LLMMessage(
            role="user",
            content=_format_user_message(current_message, is_current=True),
            name=current_message.author_name.replace(" ", "_").lower()[:32] or "user",
        )
    )

    # ── Token budget enforcement ──────────────────────────────
    budget = settings.context_max_tokens
    kept: list[LLMMessage] = []
    running = 0
    for msg in reversed(llm_messages):
        t = estimate_tokens(msg.content or "")
        if running + t > budget and kept:
            break
        kept.append(msg)
        running += t
    kept.reverse()
    llm_messages = kept

    log.debug(
        "context_built",
        channel_id=discord_channel_id,
        channel_db_id=channel_db_id,
        message_count=len(llm_messages),
        estimated_tokens=running,
        budget=budget,
        has_summary=summary_text is not None,
        memory_count=len(long_term_memories),
        style_formality=style.formality,
    )

    return LLMRequest(
        system_prompt=system_prompt,
        messages=llm_messages,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        tools=tool_definitions or [],
    )
