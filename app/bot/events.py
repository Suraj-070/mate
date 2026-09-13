"""Discord event handlers.

Thin wiring layer: discord.py event → message_handler pipeline.
Keeps event-handler code minimal; all real logic lives in message_handler.
"""
from __future__ import annotations

import discord

from app.bot.commands import sync_commands
from app.bot.message_handler import handle_message, handle_ready, maybe_trigger_summarization
from app.config.settings import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)


def register_event_handlers(
    client: discord.Client,
    tree: discord.app_commands.CommandTree | None = None,
) -> None:
    """Wire up all discord.py event handlers."""

    @client.event
    async def on_ready() -> None:
        await handle_ready(client)
        # Phase 3: hand the discord.Client to the scheduler so it can send messages
        # when reminders/timers fire.
        from app.workers.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler.set_client(client)
        # Sync slash commands to the test guild (if configured) for instant availability.
        # Falls back to global sync if no test guild configured.
        if tree is not None:
            settings = get_settings()
            if settings.discord_test_guild_id:
                guild = discord.Object(id=int(settings.discord_test_guild_id))
                await sync_commands(tree, client, guild=guild)
            else:
                await sync_commands(tree, client, guild=None)

    @client.event
    async def on_message(message: discord.Message) -> None:
        await handle_message(message, client)
        # Phase 2: maybe trigger conversation summarization (fire and forget).
        # Only for non-bot messages in real channels (not DMs).
        if not message.author.bot and message.guild is not None:
            try:
                await maybe_trigger_summarization(message.channel.id)
            except Exception as e:
                log.warning("summarizer_trigger_failed", error=str(e))

            # Phase 4: maybe trigger memory extraction
            try:
                await maybe_trigger_memory_extraction(message.channel.id, client)
            except Exception as e:
                log.warning("memory_extraction_trigger_failed", error=str(e))

    @client.event
    async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
        # Treat edits as new messages for simplicity in v1.
        # Could also ignore them — adjust if your group edits a lot.
        if before.content != after.content:
            await handle_message(after, client)

    # ── Phase 4: Reaction-based memory proposal approval ───────
    @client.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
        """Handle ✅ / ❌ reactions on memory proposal messages."""
        try:
            await _handle_proposal_reaction(payload, client)
        except Exception as e:
            log.warning("proposal_reaction_handler_failed", error=str(e))


async def _handle_proposal_reaction(
    payload: discord.RawReactionActionEvent,
    client: discord.Client,
) -> None:
    """Process a reaction on a memory proposal message.

    Only acts on:
    - ✅ reactions by admins → approve proposal → create memory row
    - ❌ reactions by admins → reject proposal → clear from pending

    Removes the bot's reactions after either action.
    """
    from app.bot.permissions import is_admin
    from app.database import repository as repo
    from app.memory.proposals import (
        approve_proposal,
        get_pending_proposal,
        reject_proposal,
    )

    # Only handle ✅ or ❌
    emoji_str = str(payload.emoji)
    if emoji_str not in ("✅", "❌", "\u2705", "\u274c"):
        return

    # Skip reactions by bots
    if payload.user_id == client.user.id if client.user else False:
        return

    proposal = get_pending_proposal(payload.message_id)
    if proposal is None:
        return  # not a proposal message

    # Permission: only admins
    discord_user_id = str(payload.user_id)
    if not await is_admin(discord_user_id):
        # Silently remove non-admin reactions to keep the message clean
        try:
            channel = client.get_channel(payload.channel_id)
            if channel is None:
                channel = await client.fetch_channel(payload.channel_id)
            if channel is not None:
                msg = await channel.fetch_message(payload.message_id)
                await msg.remove_reaction(emoji_str, payload.member or discord.Object(id=payload.user_id))
        except Exception:
            pass
        return

    # Get the admin's DB user row
    admin_user = await repo.get_or_create_user(
        discord_user_id=discord_user_id,
        username=payload.member.name if payload.member else "admin",
        display_name=payload.member.display_name if payload.member else "Admin",
        is_admin=True,
    )

    # Handle approve / reject
    if emoji_str in ("✅", "\u2705"):
        success, memory_id, err = await approve_proposal(
            proposal_message_id=payload.message_id,
            approved_by_discord_user_id=discord_user_id,
            approved_by_user_db_id=admin_user.id,
        )
        action_text = f"✅ approved as memory #{memory_id}" if success else f"❌ failed: {err}"
    else:  # ❌
        success = await reject_proposal(payload.message_id)
        action_text = "❌ dismissed" if success else "(already handled)"

    # Try to edit the proposal message to show the outcome + clear reactions
    try:
        channel = client.get_channel(payload.channel_id)
        if channel is None:
            channel = await client.fetch_channel(payload.channel_id)
        if channel is not None:
            msg = await channel.fetch_message(payload.message_id)
            # Edit content to indicate resolution
            old_content = msg.content
            new_content = f"~~{old_content}~~\n\n_{action_text}_"
            await msg.edit(content=new_content)
            # Clear bot's reactions
            try:
                await msg.clear_reactions()
            except discord.Forbidden:
                pass
    except Exception as e:
        log.warning("proposal_message_cleanup_failed", message_id=payload.message_id, error=str(e))


async def maybe_trigger_memory_extraction(
    discord_channel_id: str,
    client: discord.Client,
) -> None:
    """Periodically run memory extraction on the channel.

    Triggered on every non-bot message. The extraction itself is cheap if
    no work is needed (a count check + return). Actual extraction runs every
    N messages per channel.
    """
    from app.config.settings import get_settings
    from sqlalchemy import select as sa_select, func as sa_func

    from app.database.connection import session_scope
    from app.database.models import Channel, Message, ConversationSummary

    settings = get_settings()
    if not settings.enable_memory_extraction or not settings.enable_long_term_memory:
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

        # Count messages since the last summary (or all if none)
        last_summary_result = await session.execute(
            sa_select(ConversationSummary)
            .where(ConversationSummary.channel_id == channel_db_id)
            .order_by(ConversationSummary.created_at.desc())
            .limit(1)
        )
        last_summary = last_summary_result.scalar_one_or_none()

        if last_summary is not None:
            count_result = await session.execute(
                sa_select(sa_func.count(Message.id)).where(
                    Message.channel_id == channel_db_id,
                    Message.created_at > last_summary.created_at,
                )
            )
        else:
            count_result = await session.execute(
                sa_select(sa_func.count(Message.id)).where(Message.channel_id == channel_db_id)
            )
        msg_count = int(count_result.scalar() or 0)

    # Trigger extraction at intervals — every N messages
    if msg_count == 0 or msg_count % settings.memory_extraction_trigger_message_count != 0:
        return

    log.info(
        "memory_extraction_triggered",
        channel_db_id=channel_db_id,
        msg_count=msg_count,
        interval=settings.memory_extraction_trigger_message_count,
    )

    # Run extraction (fire and forget — don't block message handler)
    from app.memory.proposals import run_extraction
    try:
        await run_extraction(channel_db_id, discord_channel_id, client, settings)
    except Exception as e:
        log.warning("memory_extraction_run_failed", channel=discord_channel_id, error=str(e))
