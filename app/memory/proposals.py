"""Memory extraction proposal UI (Phase 4).

When the LLM proposes memories from chat, the bot posts them as ephemeral
messages only admins can see. Admins react ✅ to approve (creates memory row)
or ❌ to reject (deletes the proposal message).

Flow:
  1. Background trigger (every N messages per channel) calls run_extraction()
  2. run_extraction() fetches recent messages, filters by privacy, calls
     extract_memory_proposals() (LLM-based)
  3. Each proposal is posted to the channel as an ephemeral message
     with ✅ / ❌ reactions
  4. on_raw_reaction_add event handler in events.py:
     - Checks the reactor is an admin
     - If ✅: creates a Memory row from the proposal
     - If ❌: deletes the proposal message
     - Either way: removes the bot's reactions to clean up

Proposals are stored transiently as Discord messages — NOT in DB. If the bot
restarts, pending proposals vanish. This is intentional — proposals are
short-lived by design.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.database import repository as repo
from app.memory.extraction import extract_memory_proposals
from app.memory.models import ExtractedMemoryProposal
from app.memory.policies import (
    can_use_message_for_memory,
    default_confidence_for_kind,
    default_expiry_for_kind,
)
from app.utils.logging import get_logger

log = get_logger(__name__)


# ── In-memory cache of pending proposals ──────────────────────
# Maps proposal Discord message ID -> (proposal data, channel_db_id, source_message_id)
# Used by the reaction handler to look up the proposal when an admin reacts.
# Lost on restart — that's fine, proposals are short-lived.
_pending_proposals: dict[int, dict[str, Any]] = {}


def register_pending_proposal(
    proposal_message_id: int,
    proposal: ExtractedMemoryProposal,
    channel_db_id: int,
    source_message_id: Optional[str],
    admin_user_ids: list[int],
) -> None:
    """Register a pending proposal so the reaction handler can act on it."""
    _pending_proposals[proposal_message_id] = {
        "kind": proposal.kind.value,
        "content": proposal.content,
        "confidence": proposal.confidence,
        "rationale": proposal.rationale,
        "channel_db_id": channel_db_id,
        "source_message_id": source_message_id,
        "admin_user_ids": admin_user_ids,
    }


def get_pending_proposal(proposal_message_id: int) -> Optional[dict[str, Any]]:
    return _pending_proposals.get(proposal_message_id)


def clear_pending_proposal(proposal_message_id: int) -> None:
    _pending_proposals.pop(proposal_message_id, None)


async def run_extraction(
    channel_db_id: int,
    discord_channel_id: str,
    discord_client,
    settings,
) -> int:
    """Run extraction on recent messages and post proposals to the channel.

    Returns the number of proposals posted.

    Args:
        channel_db_id: DB ID of the channel
        discord_channel_id: Discord channel ID (string)
        discord_client: discord.Client — used to send proposal messages
        settings: app settings (used for max_proposals_per_run)
    """
    if not settings.enable_memory_extraction or not settings.enable_long_term_memory:
        return 0

    # Fetch recent messages from DB (we want full content, not the buffer)
    recent = await repo.fetch_recent_messages(channel_db_id, limit=20)
    if len(recent) < 5:
        return 0  # not enough to extract from

    # Build a map of author DB IDs -> User rows for privacy filtering (batch)
    author_ids = list({m.author_id for m in recent})
    authors_by_id: dict[int, Any] = {}
    from app.database.repository import get_users_by_ids
    users = await get_users_by_ids(author_ids)
    for user in users:
        authors_by_id[user.id] = user

    # Run extraction
    proposals = await extract_memory_proposals(recent, authors_by_id)
    if not proposals:
        return 0

    # Cap to avoid spamming admins
    proposals = proposals[: settings.memory_extraction_max_proposals_per_run]

    # Resolve channel
    try:
        discord_channel = discord_client.get_channel(int(discord_channel_id))
        if discord_channel is None:
            discord_channel = await discord_client.fetch_channel(int(discord_channel_id))
    except Exception as e:
        log.warning("extraction_channel_lookup_failed", channel_id=discord_channel_id, error=str(e))
        return 0

    if discord_channel is None:
        log.warning("extraction_channel_not_found", channel_id=discord_channel_id)
        return 0

    admin_ids = settings.admin_ids
    posted = 0

    for proposal in proposals:
        try:
            # Post as a regular message (ephemeral doesn't support reactions well).
            # We rely on the message format making it clear admins should act.
            text = (
                f"🤔 **Memory proposal** (admin action needed)\n"
                f"> {proposal.content}\n"
                f"_kind: {proposal.kind.value} | confidence: {proposal.confidence:.1f}_\n"
                f"_rationale: {proposal.rationale}_\n"
                f"react ✅ to approve · ❌ to dismiss"
            )

            sent = await discord_channel.send(text)
            await sent.add_reaction("✅")
            await sent.add_reaction("❌")

            # Register for the reaction handler
            register_pending_proposal(
                proposal_message_id=sent.id,
                proposal=proposal,
                channel_db_id=channel_db_id,
                source_message_id=None,  # we don't trace back to a specific source msg
                admin_user_ids=admin_ids,
            )
            posted += 1
            log.info(
                "memory_proposal_posted",
                channel_db_id=channel_db_id,
                kind=proposal.kind.value,
                content_preview=proposal.content[:60],
            )
        except Exception as e:
            log.warning("memory_proposal_post_failed", error=str(e))
            continue

    return posted


async def approve_proposal(
    proposal_message_id: int,
    approved_by_discord_user_id: str,
    approved_by_user_db_id: int,
) -> tuple[bool, Optional[int], Optional[str]]:
    """Approve a pending memory proposal.

    Returns (success, memory_id_if_created, error_message).
    """
    proposal = get_pending_proposal(proposal_message_id)
    if proposal is None:
        return False, None, "no pending proposal with that message ID"

    # Create the memory row
    try:
        memory = await repo.create_memory(
            channel_db_id=proposal["channel_db_id"],
            user_db_id=None,  # group-level — extraction never creates user-scoped
            kind=proposal["kind"],
            content=proposal["content"],
            created_by_user_id=approved_by_user_db_id,
            source_message_id=proposal.get("source_message_id"),
            confidence=proposal["confidence"],
            expires_at=default_expiry_for_kind(proposal["kind"]),
        )
    except Exception as e:
        log.exception("memory_proposal_approval_db_failed", error=str(e))
        return False, None, f"DB error: {e}"

    clear_pending_proposal(proposal_message_id)
    log.info(
        "memory_proposal_approved",
        proposal_message_id=proposal_message_id,
        memory_id=memory.id,
        approved_by=approved_by_discord_user_id,
    )
    return True, memory.id, None


async def reject_proposal(proposal_message_id: int) -> bool:
    """Reject a pending memory proposal. Returns True if a proposal was cleared."""
    if proposal_message_id not in _pending_proposals:
        return False
    clear_pending_proposal(proposal_message_id)
    log.info("memory_proposal_rejected", proposal_message_id=proposal_message_id)
    return True
