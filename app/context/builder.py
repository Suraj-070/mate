"""Context builder — assembles the LLM request from all sources.

Smart lazy loading:
- Tools only injected when message contains tool-related keywords
- Style profile only loaded if channel has approved samples
- Memories only queried if channel has any saved
- Topic only included if detected
- Summary only included if exists
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
_style_profile_cache: dict[int, StyleProfile] = {}

# Keywords that trigger tool loading
TOOL_KEYWORDS = [
    "remind", "reminder", "timer", "alarm",
    "schedule", "alert", "notify", "countdown", "when", "baje",
    "remind gara", "set", "cancel", "list reminders", "list timers",
]


def invalidate_style_cache(channel_db_id: int | None = None) -> None:
    if channel_db_id is None:
        _style_profile_cache.clear()
    else:
        _style_profile_cache.pop(channel_db_id, None)


def _needs_tools(message_content: str) -> bool:
    """Check if message likely needs tool use based on keywords."""
    content_lower = message_content.lower()
    return any(kw in content_lower for kw in TOOL_KEYWORDS)


def _format_user_message(msg: BufferedMessage, is_current: bool = False) -> str:
    prefix = "[now]" if is_current else ""
    return f"{prefix}[{msg.author_name}]: {msg.content}".strip()


async def build_request(
    channel_db_id: int,
    discord_channel_id: str,
    current_message: BufferedMessage,
    tool_definitions: list[dict] | None = None,
    tool_context: "ToolContext | None" = None,  # noqa: F821
    user_db_id: int | None = None,
) -> LLMRequest:
    """Assemble an LLMRequest ready to send to provider.

    Lazy loads features only when needed to save tokens.
    """
    settings = get_settings()
    buffer = get_buffer()

    # ── Smart tool loading — only when message needs it ────────
    if tool_definitions is None and settings.enable_tools:
        if _needs_tools(current_message.content):
            from app.tools.registry import get_default_registry
            try:
                registry = get_default_registry()
                tool_definitions = registry.all_schemas() if len(registry) > 0 else None
                log.debug("tools_loaded_for_message", trigger=True)
            except Exception as e:
                log.warning("tool_schema_load_failed", error=str(e))
                tool_definitions = None
        else:
            log.debug("tools_skipped", reason="no_tool_keywords_in_message")
            tool_definitions = None

    # ── Style profile — only load if channel has samples ──────
    if channel_db_id in _style_profile_cache:
        style = _style_profile_cache[channel_db_id]
    elif settings.enable_style_learning:
        style = await load_style_profile_for_channel(channel_db_id)
        _style_profile_cache[channel_db_id] = style
    else:
        style = default_style_profile()

    # ── Memories + summary — only if enabled ──────────────────
    long_term_memories: list[str] = []
    summary_text: str | None = None

    if settings.enable_long_term_memory or settings.enable_conversation_summaries:
        extras: ContextExtras = await retrieve_context_extras(channel_db_id)
        if settings.enable_long_term_memory:
            long_term_memories = memories_as_prompt_block(extras.memories)
        if settings.enable_conversation_summaries:
            summary_text = extras.summary.summary_text if extras.summary else None

    # ── Topic — only if enabled and detected ──────────────────
    topic_block: str | None = None
    if settings.enable_topic_tracking:
        topic = get_channel_topic(discord_channel_id)
        topic_block = topic_as_prompt_block(topic)

    # ── User language — only if enabled and user exists ───────
    user_language_hint: str | None = None
    if settings.enable_per_user_language and user_db_id is not None:
        try:
            from app.database.repository import get_user_by_id
            user = await get_user_by_id(user_db_id)
            if user is not None and user.preferred_language:
                user_language_hint = user.preferred_language
        except Exception as e:
            log.warning("user_language_lookup_failed", error=str(e), user_db_id=user_db_id)

    # ── Build system prompt ────────────────────────────────────
    system_prompt = build_system_prompt(
        personality=default_personality(),
        style=style,
        tool_definitions=tool_definitions,
        long_term_memories=long_term_memories,
        conversation_summary=summary_text,
        topic_block=topic_block,
        user_language_hint=user_language_hint,
    )

    # ── Build conversation window ──────────────────────────────
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

    # Append current message as final user turn
    llm_messages.append(
        LLMMessage(
            role="user",
            content=_format_user_message(current_message, is_current=True),
            name=current_message.author_name.replace(" ", "_").lower()[:32] or "user",
        )
    )

    # ── Token budget enforcement ───────────────────────────────
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
        tools_loaded=tool_definitions is not None,
    )

    return LLMRequest(
        system_prompt=system_prompt,
        messages=llm_messages,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        tools=tool_definitions or [],
    )