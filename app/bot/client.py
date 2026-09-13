"""Discord client factory + intent configuration.

Keeps the discord.Client construction in one place so we can tune
intents, cache flags, and member lookup behavior centrally.
"""
from __future__ import annotations

import discord

from app.bot.commands import register_commands, sync_commands
from app.bot.events import register_event_handlers
from app.config.settings import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)


def make_bot() -> tuple[discord.Client, discord.app_commands.CommandTree]:
    """Build the configured discord.Client + CommandTree.

    Returns:
        (client, tree) — the tree is used by events.py to sync commands
        when the bot becomes ready.

    Intents:
    - message_content: REQUIRED for reading message text. This is a "privileged"
      intent — must be enabled in the Discord Developer Portal too.
    - guilds: needed for channel/guild events.
    - messages: needed for message cache and reply references.
    - members: needed for fetching member info (display names).
    - reactions: needed for Phase 2 (admin reaction-based style approval).
    """
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    intents.members = True
    intents.reactions = True

    client = discord.Client(
        intents=intents,
        max_messages=None,
    )
    tree = discord.app_commands.CommandTree(client)

    # Wire up event handlers (Phase 1 message pipeline + Phase 2 hooks)
    register_event_handlers(client, tree)

    # Register slash commands + context menus (Phase 2)
    register_commands(tree, client)

    return client, tree
