"""Message handler — the main pipeline for every Discord message.

This is the orchestrator of the conversation system. It:
  1. Pre-filters (bots, opt-outs, disabled channels)
  2. Persists the message (always — for audit + buffer rebuild)
  3. Updates the in-memory conversation buffer
  4. Asks response_policy whether to reply, react, or stay silent
  5. RESPOND: builds context, calls LLM, post-processes, sends reply
     REACT:  picks an emoji and adds a reaction
     IGNORE: returns silently
  6. Persists the bot's reply (if any), updates buffer, sets cooldown

Critical: this function MUST NOT raise. Any exception is logged and the
bot stays silent. The bot crashing on a single bad message is the worst
possible UX.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict

import discord

from app.ai.orchestrator import generate_response
from app.ai.response_policy import (
    decide,
    llm_assisted_decide,
    looks_addressed_to_bot,
    looks_like_question,
)
from app.ai.schemas import DecisionInput, DecisionOutcome
from app.bot.permissions import (
    is_bot_mentioned,
    is_opted_out_of_reply,
    is_reply_to_bot,
)
from app.bot.reactions import pick_reaction
from app.context.builder import build_request
from app.context.conversation_buffer import BufferedMessage, get_buffer
from app.database.repository import (
    get_active_summary,
    get_or_create_bot_user,
    get_or_create_channel,
    get_or_create_user,
    is_channel_enabled,
    log_message,
)
from app.memory.summarizer import maybe_summarize_channel
from app.utils.logging import get_logger
from app.utils.text import strip_ai_isms, truncate_for_discord

log = get_logger(__name__)


# Per-channel cooldown tracker (in-memory; reset on restart)
@dataclass
class ChannelState:
    last_reply_ts: float = 0.0
    replies_this_minute: int = 0
    minute_window_start: float = 0.0


_channel_states: Dict[str, ChannelState] = {}


def _get_state(channel_id: str) -> ChannelState:
    return _channel_states.setdefault(channel_id, ChannelState())


def _check_rate_limit(channel_id: str, settings) -> tuple[bool, bool]:
    """Returns (cooldown_active, rate_limit_exceeded)."""
    state = _get_state(channel_id)
    now = time.time()
    # Reset minute window if expired
    if now - state.minute_window_start >= 60.0:
        state.minute_window_start = now
        state.replies_this_minute = 0
    cooldown_active = (now - state.last_reply_ts) < settings.reply_cooldown_seconds
    rate_limit_exceeded = state.replies_this_minute >= settings.max_replies_per_minute
    return cooldown_active, rate_limit_exceeded


def _mark_replied(channel_id: str) -> None:
    state = _get_state(channel_id)
    state.last_reply_ts = time.time()
    state.replies_this_minute += 1


async def handle_ready(client: discord.Client) -> None:
    """Called when the bot is logged in and ready."""
    log.info(
        "bot_ready",
        user=str(client.user),
        guilds=len(client.guilds),
        bot_id=client.user.id if client.user else None,
    )
    if client.user:
        print(f"✅ Bot logged in as {client.user} (ID: {client.user.id})")
        print(f"   Connected to {len(client.guilds)} guild(s)")
        print("   Press Ctrl+C to shut down.")


async def handle_message(message: discord.Message, client: discord.Client) -> None:
    """Main pipeline. See module docstring."""
    try:
        await _handle_message_safe(message, client)
    except Exception as e:
        log.exception("message_handler_unexpected_error", error=str(e))
        # Never crash the bot — swallow and continue


async def _handle_message_safe(message: discord.Message, client: discord.Client) -> None:
    from app.config.settings import get_settings

    settings = get_settings()

    # ── Pre-filter: ignore bots (including ourselves) ──────────
    if message.author.bot:
        return

    # ── Pre-filter: DMs? ────────────────────────────────────────
    if message.guild is None:
        # Allow DMs only if mentioned — gives users a private way to talk to bot
        if not is_bot_mentioned(message, client.user):
            return

    # ── Pre-filter: channel enabled? ───────────────────────────
    channel_id_str = str(message.channel.id)
    if not await is_channel_enabled(channel_id_str):
        return

    # ── Pre-filter: author opt-out? ────────────────────────────
    if await is_opted_out_of_reply(str(message.author.id)):
        return

    # ── Persist channel + user + message ───────────────────────
    guild_id = str(message.guild.id) if message.guild else "dm"
    channel_name = (
        getattr(message.channel, "name", None) or f"dm-{message.author.id}"
    )
    channel = await get_or_create_channel(guild_id, channel_id_str, channel_name)
    user = await get_or_create_user(
        str(message.author.id),
        username=message.author.name,
        display_name=message.author.display_name or message.author.name,
    )
    await log_message(
        channel_db_id=channel.id,
        author_db_id=user.id,
        discord_message_id=str(message.id),
        content=message.content,
        is_bot=False,
        reply_to_message_id=(
            str(message.reference.message_id) if message.reference else None
        ),
    )

    # ── Update conversation buffer ─────────────────────────────
    author_display = message.author.display_name or message.author.name
    buffered = BufferedMessage(
        discord_message_id=str(message.id),
        author_id=str(message.author.id),
        author_name=author_display,
        content=message.content,
        is_bot=False,
        reply_to=message.reference.message_id if message.reference else None,
    )
    buffer = get_buffer()
    buffer.append(channel_id_str, buffered)

    # ── Response decision ──────────────────────────────────────
    cooldown_active, rate_limit_exceeded = _check_rate_limit(channel_id_str, settings)

    is_mention = is_bot_mentioned(message, client.user) if client.user else False
    is_reply_bot = is_reply_to_bot(message, client.user) if client.user else False

    bot_spoke_recently = buffer.bot_spoke_in_last(
        channel_id_str, n_messages=5, seconds=300.0
    )
    is_question = looks_like_question(message.content)
    addressed_by_name = looks_addressed_to_bot(
        message.content,
        bot_name=client.user.display_name if client.user else None,
    )

    decision_input = DecisionInput(
        is_direct_mention=is_mention,
        is_reply_to_bot=is_reply_bot,
        bot_spoke_recently=bot_spoke_recently,
        is_question=is_question,
        addressed_to_bot_by_name=addressed_by_name,
        channel_autonomy_enabled=settings.enable_autonomous_participation,
        cooldown_active=cooldown_active,
        rate_limit_exceeded=rate_limit_exceeded,
        author_opted_out=False,  # already filtered above
        message_length=len(message.content),
        message_text=message.content,
        enable_react_outcome=settings.enable_react_outcome,
    )
    decision = decide(decision_input)

    # ── Phase 2: LLM-assisted decision for gray zone (optional) ─
    # Triggered only when:
    #   - LLM assist is enabled
    #   - Autonomy is on
    #   - Bot was recently speaking
    #   - Not a direct mention or reply
    #   - The rule-based decision returned IGNORE (no clear question)
    if (
        decision.outcome == DecisionOutcome.IGNORE
        and settings.enable_llm_decision_assist
        and settings.enable_autonomous_participation
        and bot_spoke_recently
        and not is_mention
        and not is_reply_bot
    ):
        # Fetch current summary for context if available
        summary_text = ""
        try:
            summary = await get_active_summary(channel.id)
            if summary:
                summary_text = summary.summary_text
        except Exception:
            pass
        decision = await llm_assisted_decide(
            inp=decision_input,
            message_text=message.content,
            recent_context_summary=summary_text,
        )

    log.debug(
        "message_decision",
        channel=channel_name,
        author=author_display,
        outcome=decision.outcome.value,
        reason=decision.reason,
        is_mention=is_mention,
        is_reply_bot=is_reply_bot,
        is_question=is_question,
        addressed_by_name=addressed_by_name,
    )

    if decision.outcome == DecisionOutcome.IGNORE:
        return

    # ── REACT outcome: add an emoji reaction (no reply text) ────
    if decision.outcome == DecisionOutcome.REACT:
        try:
            emoji = pick_reaction(message.content)
            await message.add_reaction(emoji)
            log.info(
                "bot_reacted",
                channel=channel_name,
                emoji=emoji,
                message_id=str(message.id),
            )
        except discord.HTTPException as e:
            log.warning("react_failed", error=str(e))
        return

    # ── RESPOND: build LLM request (Phase 2 needs channel_db_id + discord_channel_id) ─
    # Phase 3: build a ToolContext so the orchestrator can dispatch tool calls
    from app.tools.schemas import ToolContext
    tool_ctx = ToolContext(
        user_db_id=user.id,
        discord_user_id=str(message.author.id),
        user_display_name=author_display,
        is_admin=user.is_admin,
        channel_db_id=channel.id,
        discord_channel_id=channel_id_str,
        discord_guild_id=guild_id if message.guild else None,
    )

    llm_request = await build_request(
        channel_db_id=channel.id,
        discord_channel_id=channel_id_str,
        current_message=buffered,
        tool_context=tool_ctx,
        user_db_id=user.id,  # Phase 4: for per-user language preference
    )

    # ── Generate reply (Phase 3: passes tool_context for permission checks) ─
    reply_text = await generate_response(llm_request, tool_context=tool_ctx)

    # ── Post-process ───────────────────────────────────────────
    reply_text = strip_ai_isms(reply_text)
    if not reply_text:
        log.info("empty_reply_after_processing", channel=channel_name)
        return

    # ── Send (handle Discord 2000-char limit) ──────────────────
    chunks = truncate_for_discord(reply_text, max_chars=2000)
    sent_message_ids: list[str] = []
    is_first = True
    for chunk in chunks:
        sent = await message.channel.send(
            chunk,
            reference=(message if is_first else None),
            mention_author=False,
        )
        sent_message_ids.append(str(sent.id))
        is_first = False

    # ── Persist bot reply + update buffer + cooldown ───────────
    bot_display = client.user.display_name if client.user else "Bot"
    bot_user_row = await get_or_create_bot_user(
        str(client.user.id) if client.user else "0",
        display_name=bot_display,
    )
    for sent_id in sent_message_ids:
        await log_message(
            channel_db_id=channel.id,
            author_db_id=bot_user_row.id,
            discord_message_id=sent_id,
            content=reply_text[:1000],  # truncate for storage safety
            is_bot=True,
            reply_to_message_id=str(message.id),
        )
        buffer.append(
            channel_id_str,
            BufferedMessage(
                discord_message_id=sent_id,
                author_id=str(client.user.id) if client.user else "bot",
                author_name=bot_display,
                content=reply_text,
                is_bot=True,
                reply_to=str(message.id),
            ),
        )
        _mark_replied(channel_id_str)


# ── Phase 2 background trigger ────────────────────────────────
async def maybe_trigger_summarization(discord_channel_id: str) -> None:
    """Check if the channel needs summarization; if so, summarize.

    Called from events.py on every non-bot message. The summarizer itself
    is cheap if no work is needed (just a count + a return). Real summarization
    only happens every N messages.
    """
    from app.config.settings import get_settings
    from sqlalchemy import select as sa_select

    from app.database.connection import session_scope
    from app.database.models import Channel

    settings = get_settings()
    if not settings.enable_conversation_summaries:
        return

    # Look up the channel DB ID
    async with session_scope() as session:
        result = await session.execute(
            sa_select(Channel).where(Channel.discord_channel_id == discord_channel_id)
        )
        channel = result.scalar_one_or_none()
        if channel is None:
            return
        channel_db_id = channel.id

    try:
        await maybe_summarize_channel(
            channel_db_id=channel_db_id,
            discord_channel_id=discord_channel_id,
        )
    except Exception as e:
        log.warning("summarization_failed", channel=discord_channel_id, error=str(e))
