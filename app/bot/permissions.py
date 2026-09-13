"""Permission checks for the bot layer.

Centralizes all "is this user allowed to do X" logic so it's auditable
in one place. NEVER inline permission checks in event handlers.
"""
from __future__ import annotations

from app.config.settings import get_settings
from app.database.repository import get_user_by_discord_id


async def is_admin(discord_user_id: str) -> bool:
    """Check admin status from settings.ADMIN_IDS first, then DB."""
    settings = get_settings()
    try:
        uid = int(discord_user_id)
    except (ValueError, TypeError):
        return False
    if uid in settings.admin_ids:
        return True
    user = await get_user_by_discord_id(discord_user_id)
    return bool(user and user.is_admin)


async def is_opted_out_of_reply(discord_user_id: str) -> bool:
    user = await get_user_by_discord_id(discord_user_id)
    return bool(user and user.opt_out_reply)


async def is_opted_out_of_memory(discord_user_id: str) -> bool:
    user = await get_user_by_discord_id(discord_user_id)
    return bool(user and user.opt_out_memory)


async def is_opted_out_of_style(discord_user_id: str) -> bool:
    user = await get_user_by_discord_id(discord_user_id)
    return bool(user and user.opt_out_style)


def is_bot_mentioned(message, bot_user) -> bool:
    """Check if the bot is mentioned in the message."""
    if not message.mentions:
        return False
    return any(m.id == bot_user.id for m in message.mentions)


def is_reply_to_bot(message, bot_user) -> bool:
    """Check if the message is a reply to one of the bot's messages."""
    if message.reference is None or message.reference.resolved is None:
        return False
    resolved = message.reference.resolved
    # resolved may be a Message or a PartialMessage; check author.id safely
    author = getattr(resolved, "author", None)
    return author is not None and author.id == bot_user.id
