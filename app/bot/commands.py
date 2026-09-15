"""Slash command + context menu registration.

All discord.py app_commands live here. The handler functions do permission
checks, then delegate to repository functions for actual work.

Commands implemented:
  /remember <kind> <text> [expires_in_days]   (admin only)
  /forget <id>                                 (admin only)
  /memories list [channel]                     (admin only for user-scoped; everyone for group)
  /privacy <flag> <on|off>                     (self only)
  /style status                                (admin only)
  /style samples [count]                       (admin only)
  /style rebuild                               (admin only)

Context menus (right-click on a message):
  "Use for Style"     (admin only)  — add message as style sample
  "Remember this"     (admin only)  — propose memory from message content
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from app.bot.permissions import is_admin
from app.config.settings import get_settings
from app.context.builder import invalidate_style_cache
from app.database import repository as repo
from app.memory.models import MemoryKind
from app.memory.policies import (
    can_use_message_for_style,
    default_confidence_for_kind,
    default_expiry_for_kind,
    looks_sensitive,
)
from app.personality.style_profile import rebuild_style_profile
from app.utils.logging import get_logger

log = get_logger(__name__)


def register_commands(tree: app_commands.CommandTree, client: discord.Client) -> None:
    """Register all slash commands and context menus on the given tree."""

    # ── /bye ─────────────────────────────────────────────────
    @tree.command(
        name="bye",
        description="Send a sweet goodbye 💕",
    )
    async def bye_command(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Byeee byeeeee babe😚😚💞💕🧿\n"
            "Have an amazing day like u\n"
            "Seee yaaa\n"
            "Muahhhhh😚😚😚💕💞"
        )

    # ── /remember ────────────────────────────────────────────
    @tree.command(
        name="remember",
        description="Store a long-term memory (admin only)",
    )
    @app_commands.describe(
        kind="Type of memory",
        text="What to remember (short, paraphrased)",
        expires_in_days="Optional: forget after N days (default: by kind)",
    )
    @app_commands.choices(kind=[
        app_commands.Choice(name="fact", value="fact"),
        app_commands.Choice(name="preference", value="preference"),
        app_commands.Choice(name="lore", value="lore"),
        app_commands.Choice(name="event", value="event"),
        app_commands.Choice(name="joke", value="joke"),
    ])
    async def remember_cmd(
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        text: str,
        expires_in_days: Optional[int] = None,
    ) -> None:
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "only admins can use this command", ephemeral=True
            )
            return

        settings = get_settings()
        if not settings.enable_long_term_memory:
            await interaction.response.send_message(
                "long-term memory is disabled in config", ephemeral=True
            )
            return

        # Privacy check — refuse anything that looks like PII
        if looks_sensitive(text):
            await interaction.response.send_message(
                "that text contains something that looks like sensitive info "
                "(phone/email/token). please rephrase without the sensitive bits.",
                ephemeral=True,
            )
            return

        # Channel scope: this memory is scoped to the current channel (or global if DM)
        channel_db_id: Optional[int] = None
        if interaction.channel is not None:
            chan = await repo.get_or_create_channel(
                guild_id=str(interaction.guild.id) if interaction.guild else "dm",
                channel_id=str(interaction.channel.id),
                name=getattr(interaction.channel, "name", "dm") or "dm",
            )
            channel_db_id = chan.id

        # Get the admin user row
        admin_user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            is_admin=True,
        )

        kind_str = kind.value
        confidence = default_confidence_for_kind(kind_str)
        expiry = default_expiry_for_kind(kind_str)
        if expires_in_days is not None:
            from datetime import datetime, timedelta, timezone
            expiry = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

        memory = await repo.create_memory(
            channel_db_id=channel_db_id,
            user_db_id=None,  # group-level memory
            kind=kind_str,
            content=text,
            created_by_user_id=admin_user.id,
            confidence=confidence,
            expires_at=expiry,
        )

        await interaction.response.send_message(
            f"got it — remembered as `{kind_str}` (id: {memory.id}, confidence: {confidence})",
            ephemeral=True,
        )

    # ── /forget ───────────────────────────────────────────────
    @tree.command(
        name="forget",
        description="Delete a memory by ID (admin only)",
    )
    @app_commands.describe(memory_id="The memory ID to forget")
    async def forget_cmd(
        interaction: discord.Interaction,
        memory_id: int,
    ) -> None:
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "only admins can use this command", ephemeral=True
            )
            return

        deleted = await repo.delete_memory(memory_id)
        if deleted:
            await interaction.response.send_message(
                f"forgot memory #{memory_id}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"no active memory found with id {memory_id}", ephemeral=True
            )

    # ── /memories ─────────────────────────────────────────────
    @tree.command(
        name="memories",
        description="List stored memories",
    )
    @app_commands.describe(scope="which memories to list")
    @app_commands.choices(scope=[
        app_commands.Choice(name="this channel", value="channel"),
        app_commands.Choice(name="all", value="all"),
    ])
    async def memories_cmd(
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] = None,  # type: ignore[assignment]
    ) -> None:
        if scope is None:
            scope = app_commands.Choice(name="this channel", value="channel")
        # Anyone can list group-level memories (no user-scoped ones ever shown)
        channel_db_id: Optional[int] = None
        if scope.value == "channel":
            if interaction.channel is None:
                await interaction.response.send_message(
                    "this scope requires a channel context", ephemeral=True
                )
                return
            chan = await repo.get_or_create_channel(
                guild_id=str(interaction.guild.id) if interaction.guild else "dm",
                channel_id=str(interaction.channel.id),
                name=getattr(interaction.channel, "name", "dm") or "dm",
            )
            channel_db_id = chan.id

        memories = await repo.list_memories(
            channel_db_id=channel_db_id,
            include_user_scoped=False,
            limit=20,
        )

        if not memories:
            await interaction.response.send_message(
                "no memories stored yet"
                + (" for this channel" if scope.value == "channel" else " globally"),
                ephemeral=True,
            )
            return

        lines = [f"**Memories** ({scope.value}):\n"]
        for m in memories[:15]:  # cap to keep under Discord 2000 char limit
            scope_tag = f"ch:{m.channel_id}" if m.channel_id else "global"
            lines.append(f"`#{m.id}` [{m.kind}] {m.content} ({scope_tag}, conf {m.confidence:.1f})")
        if len(memories) > 15:
            lines.append(f"... and {len(memories) - 15} more")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /privacy ──────────────────────────────────────────────
    @tree.command(
        name="privacy",
        description="Control what the bot remembers about you",
    )
    @app_commands.describe(
        flag="Which privacy flag to toggle",
        state="On (opt out) or off (opt back in)",
    )
    @app_commands.choices(flag=[
        app_commands.Choice(name="style learning", value="style"),
        app_commands.Choice(name="long-term memory", value="memory"),
        app_commands.Choice(name="bot replies", value="reply"),
    ])
    @app_commands.choices(state=[
        app_commands.Choice(name="opt out", value="on"),
        app_commands.Choice(name="opt back in", value="off"),
    ])
    async def privacy_cmd(
        interaction: discord.Interaction,
        flag: app_commands.Choice[str],
        state: app_commands.Choice[str],
    ) -> None:
        # Make sure the user exists
        await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        updated = await repo.set_user_opt_out(
            discord_user_id=str(interaction.user.id),
            flag=flag.value,
            value=(state.value == "on"),
        )
        if updated:
            await interaction.response.send_message(
                f"updated — you've opted {'OUT of' if state.value == 'on' else 'BACK INTO'} {flag.value}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "couldn't find your user record — try sending a message first",
                ephemeral=True,
            )

    # ── /language ─────────────────────────────────────────────
    @tree.command(
        name="language",
        description="Set your preferred reply language (Phase 4)",
    )
    @app_commands.describe(
        action="set, clear, or show your current preference",
        language="Language code (only needed for 'set')",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="set", value="set"),
        app_commands.Choice(name="clear", value="clear"),
        app_commands.Choice(name="show", value="show"),
    ])
    @app_commands.choices(language=[
        app_commands.Choice(name="English", value="en"),
        app_commands.Choice(name="Nepali (Devanagari)", value="ne-deva"),
        app_commands.Choice(name="Nepali (Romanized)", value="ne-roman"),
        app_commands.Choice(name="Mixed (auto-detect per message)", value="mixed"),
    ])
    async def language_cmd(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        language: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        settings = get_settings()
        if not settings.enable_per_user_language:
            await interaction.response.send_message(
                "per-user language preferences are disabled in config",
                ephemeral=True,
            )
            return

        # Make sure user exists
        await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        if action.value == "show":
            current = await repo.get_user_preferred_language(str(interaction.user.id))
            if current is None:
                await interaction.response.send_message(
                    "you haven't set a preferred language — bot will auto-detect per message",
                    ephemeral=True,
                )
            else:
                labels = {
                    "en": "English",
                    "ne-deva": "Nepali (Devanagari)",
                    "ne-roman": "Nepali (Romanized)",
                    "mixed": "Mixed (auto-detect)",
                }
                await interaction.response.send_message(
                    f"your preferred language: **{labels.get(current, current)}**",
                    ephemeral=True,
                )
            return

        if action.value == "clear":
            ok = await repo.set_user_preferred_language(
                str(interaction.user.id), None
            )
            if ok:
                await interaction.response.send_message(
                    "cleared — bot will auto-detect per message",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "couldn't find your user record",
                    ephemeral=True,
                )
            return

        # action == "set"
        if language is None:
            await interaction.response.send_message(
                "you need to pick a language to set",
                ephemeral=True,
            )
            return

        try:
            ok = await repo.set_user_preferred_language(
                str(interaction.user.id), language.value
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if ok:
            labels = {
                "en": "English",
                "ne-deva": "Nepali (Devanagari)",
                "ne-roman": "Nepali (Romanized)",
                "mixed": "Mixed",
            }
            label = labels.get(language.value, language.value)
            await interaction.response.send_message(
                f"set — bot will reply to you in **{label}** "
                f"(use `/language clear` to undo)",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "couldn't find your user record",
                ephemeral=True,
            )

    # ── /style ────────────────────────────────────────────────
    style_group = app_commands.Group(
        name="style",
        description="Manage the group's style profile (admin only)",
    )

    @style_group.command(name="status", description="Show current style profile for this channel")
    async def style_status_cmd(interaction: discord.Interaction) -> None:
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "only admins can use this command", ephemeral=True
            )
            return

        if interaction.channel is None:
            await interaction.response.send_message(
                "this command requires a channel context", ephemeral=True
            )
            return

        chan = await repo.get_or_create_channel(
            guild_id=str(interaction.guild.id) if interaction.guild else "dm",
            channel_id=str(interaction.channel.id),
            name=getattr(interaction.channel, "name", "dm") or "dm",
        )

        profile_row = await repo.get_style_profile(chan.id)
        unused_count = await repo.count_unused_style_samples(chan.id)
        total_samples = await repo.count_unused_style_samples(chan.id) + (
            (profile_row.sample_count if profile_row else 0) - unused_count
        )

        if profile_row is None:
            await interaction.response.send_message(
                f"no style profile for this channel yet\n"
                f"approved samples: {total_samples} ({unused_count} new, awaiting rebuild)",
                ephemeral=True,
            )
            return

        from app.personality.style_profile import StyleProfile
        profile = StyleProfile.from_json(profile_row.profile_json)
        lines = [
            "**Style profile for this channel:**",
            f"- formality: `{profile.formality}`",
            f"- avg reply length: `{profile.average_response_length}`",
            f"- humor: `{profile.humor_level}`",
            f"- emoji usage: `{profile.emoji_usage}`",
            f"- language: `{profile.language_behavior}`",
            f"- samples used: {profile_row.sample_count} ({unused_count} new, awaiting rebuild)",
            f"- last updated: {profile_row.updated_at}",
        ]
        if profile.common_expressions:
            lines.append(f"- common expressions: {', '.join(profile.common_expressions[:6])}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @style_group.command(name="rebuild", description="Force rebuild style profile from samples")
    async def style_rebuild_cmd(interaction: discord.Interaction) -> None:
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "only admins can use this command", ephemeral=True
            )
            return

        if interaction.channel is None:
            await interaction.response.send_message(
                "this command requires a channel context", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        chan = await repo.get_or_create_channel(
            guild_id=str(interaction.guild.id) if interaction.guild else "dm",
            channel_id=str(interaction.channel.id),
            name=getattr(interaction.channel, "name", "dm") or "dm",
        )

        new_profile = await rebuild_style_profile(chan.id, force=True)
        if new_profile is None:
            await interaction.followup.send(
                "couldn't rebuild — no samples found, or style learning is disabled",
                ephemeral=True,
            )
            return

        invalidate_style_cache(chan.id)
        await interaction.followup.send(
            f"style profile rebuilt\n"
            f"  formality: `{new_profile.formality}`\n"
            f"  humor: `{new_profile.humor_level}`\n"
            f"  emoji: `{new_profile.emoji_usage}`\n"
            f"  language: `{new_profile.language_behavior}`",
            ephemeral=True,
        )

    tree.add_command(style_group)

    # ── Context menu: Use for Style ──────────────────────────
    @app_commands.context_menu(name="Use for Style")
    async def use_for_style_ctx(
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "only admins can use this command", ephemeral=True
            )
            return

        if message.author.bot:
            await interaction.response.send_message(
                "can't use bot messages as style samples", ephemeral=True
            )
            return

        settings = get_settings()
        if not settings.enable_style_learning:
            await interaction.response.send_message(
                "style learning is disabled in config", ephemeral=True
            )
            return

        # Resolve channel
        chan = await repo.get_or_create_channel(
            guild_id=str(message.guild.id) if message.guild else "dm",
            channel_id=str(message.channel.id),
            name=getattr(message.channel, "name", "dm") or "dm",
        )

        # Find the message row
        msg_row = await repo.fetch_message_by_discord_id(chan.id, str(message.id))
        if msg_row is None:
            await interaction.response.send_message(
                "that message isn't in my log yet — try again in a moment",
                ephemeral=True,
            )
            return

        # Privacy check: was the author opted out of style learning?
        author = await repo.get_or_create_user(
            discord_user_id=str(message.author.id),
            username=message.author.name,
            display_name=message.author.display_name,
        )
        if not can_use_message_for_style(msg_row, author):
            await interaction.response.send_message(
                "that user has opted out of style learning — can't use their messages",
                ephemeral=True,
            )
            return

        # Get admin user row
        admin_user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            is_admin=True,
        )

        sample = await repo.add_style_sample(
            channel_db_id=chan.id,
            source_message_db_id=msg_row.id,
            approved_by_user_id=admin_user.id,
        )

        await interaction.response.send_message(
            f"added as style sample (id: {sample.id})\n"
            f"the profile will rebuild automatically once {settings.style_profile_rebuild_threshold} new samples accumulate",
            ephemeral=True,
        )

        # Try to trigger a rebuild — fire and forget
        try:
            new_profile = await rebuild_style_profile(chan.id, force=False)
            if new_profile is not None:
                invalidate_style_cache(chan.id)
        except Exception as e:
            log.warning("style_rebuild_after_sample_failed", error=str(e))

    tree.add_command(use_for_style_ctx)

    # ── Context menu: Remember this ──────────────────────────
    @app_commands.context_menu(name="Remember this")
    async def remember_this_ctx(
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "only admins can use this command", ephemeral=True
            )
            return

        settings = get_settings()
        if not settings.enable_long_term_memory:
            await interaction.response.send_message(
                "long-term memory is disabled in config", ephemeral=True
            )
            return

        if message.author.bot:
            await interaction.response.send_message(
                "can't remember bot messages", ephemeral=True
            )
            return

        content = message.content.strip()
        if not content:
            await interaction.response.send_message(
                "that message has no text content", ephemeral=True
            )
            return

        if looks_sensitive(content):
            await interaction.response.send_message(
                "that message contains something that looks like sensitive info. "
                "if you really want this remembered, use `/remember` and paraphrase it.",
                ephemeral=True,
            )
            return

        # Resolve channel
        chan = await repo.get_or_create_channel(
            guild_id=str(message.guild.id) if message.guild else "dm",
            channel_id=str(message.channel.id),
            name=getattr(message.channel, "name", "dm") or "dm",
        )

        # Get admin user row
        admin_user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            is_admin=True,
        )

        # Default kind: fact, confidence: 0.8 (slightly lower than admin-stated)
        memory = await repo.create_memory(
            channel_db_id=chan.id,
            user_db_id=None,
            kind="fact",
            content=content,
            created_by_user_id=admin_user.id,
            source_message_id=str(message.id),
            confidence=0.8,
            expires_at=default_expiry_for_kind("fact"),
        )

        await interaction.response.send_message(
            f"remembered (id: {memory.id}, kind: fact, confidence: 0.8)\n"
            f"use `/forget {memory.id}` to remove",
            ephemeral=True,
        )

    tree.add_command(remember_this_ctx)


    # ── /reminders ─────────────────────────────────────────────
    reminders_group = app_commands.Group(
        name="reminders",
        description="List and cancel your reminders",
    )

    @reminders_group.command(name="list", description="List your pending reminders")
    @app_commands.describe(scope="Which reminders to list")
    @app_commands.choices(scope=[
        app_commands.Choice(name="mine", value="mine"),
        app_commands.Choice(name="this channel", value="channel"),
    ])
    async def reminders_list_cmd(
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] = None,  # type: ignore[assignment]
    ) -> None:
        if scope is None:
            scope = app_commands.Choice(name="mine", value="mine")
        from app.database import repository as repo
        from app.tools.reminders.service import format_display_time

        if interaction.channel is None:
            await interaction.response.send_message("requires a channel context", ephemeral=True)
            return

        chan = await repo.get_or_create_channel(
            guild_id=str(interaction.guild.id) if interaction.guild else "dm",
            channel_id=str(interaction.channel.id),
            name=getattr(interaction.channel, "name", "dm") or "dm",
        )
        user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        if scope.value == "channel":
            if not await is_admin(str(interaction.user.id)):
                await interaction.response.send_message("only admins can list all reminders in a channel", ephemeral=True)
                return
            reminders = await repo.list_pending_reminders(channel_db_id=chan.id, limit=20)
            label = "reminders in this channel"
        else:
            reminders = await repo.list_pending_reminders(user_db_id=user.id, limit=20)
            label = "your reminders"

        if not reminders:
            await interaction.response.send_message(f"no {label} pending", ephemeral=True)
            return

        lines = [f"**{label}:**"]
        for r in reminders[:15]:
            tag = f" [{r.recurrence}]" if r.recurrence else ""
            lines.append(f"`#{r.id}` {format_display_time(r.trigger_at)}{tag} — {r.message}")
        if len(reminders) > 15:
            lines.append(f"... and {len(reminders) - 15} more")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @reminders_group.command(name="cancel", description="Cancel a reminder by ID")
    @app_commands.describe(reminder_id="The reminder ID to cancel (from /reminders list)")
    async def reminders_cancel_cmd(
        interaction: discord.Interaction,
        reminder_id: int,
    ) -> None:
        from app.database import repository as repo
        from app.workers.scheduler import get_scheduler

        user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        reminder = await repo.get_reminder(reminder_id)
        if reminder is None or reminder.is_fired or reminder.is_cancelled:
            await interaction.response.send_message(
                f"no active reminder with id #{reminder_id}", ephemeral=True
            )
            return

        if reminder.user_id != user.id and not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "you can only cancel your own reminders", ephemeral=True
            )
            return

        ok = await repo.cancel_reminder(reminder_id)
        scheduler = get_scheduler()
        if scheduler is not None and ok:
            await scheduler.unschedule_reminder(reminder_id)

        if ok:
            await interaction.response.send_message(f"cancelled reminder #{reminder_id}", ephemeral=True)
        else:
            await interaction.response.send_message("couldn't cancel — it may have already fired", ephemeral=True)

    tree.add_command(reminders_group)


    # ── /timers ───────────────────────────────────────────────
    timers_group = app_commands.Group(
        name="timers",
        description="List and cancel your timers",
    )

    @timers_group.command(name="list", description="List your active timers")
    @app_commands.describe(scope="Which timers to list")
    @app_commands.choices(scope=[
        app_commands.Choice(name="mine", value="mine"),
        app_commands.Choice(name="this channel", value="channel"),
    ])
    async def timers_list_cmd(
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] = None,  # type: ignore[assignment]
    ) -> None:
        if scope is None:
            scope = app_commands.Choice(name="mine", value="mine")
        from datetime import datetime, timezone
        from app.database import repository as repo
        from app.tools.timers.service import format_duration

        if interaction.channel is None:
            await interaction.response.send_message("requires a channel context", ephemeral=True)
            return

        chan = await repo.get_or_create_channel(
            guild_id=str(interaction.guild.id) if interaction.guild else "dm",
            channel_id=str(interaction.channel.id),
            name=getattr(interaction.channel, "name", "dm") or "dm",
        )
        user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        if scope.value == "channel":
            if not await is_admin(str(interaction.user.id)):
                await interaction.response.send_message("only admins can list all timers in a channel", ephemeral=True)
                return
            timers = await repo.list_pending_timers(channel_db_id=chan.id, limit=20)
            label = "timers in this channel"
        else:
            timers = await repo.list_pending_timers(user_db_id=user.id, limit=20)
            label = "your timers"

        if not timers:
            await interaction.response.send_message(f"no {label} active", ephemeral=True)
            return

        now_utc = datetime.now(timezone.utc)
        lines = [f"**{label}:**"]
        for t in timers[:15]:
            # SQLite may return tz-naive datetimes — coerce to UTC
            expires_at = t.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            remaining = (expires_at - now_utc).total_seconds()
            if remaining < 0:
                rem_str = "(overdue)"
            else:
                rem_str = f"{int(remaining // 60)}m{int(remaining % 60)}s left"
            tag = f" [{t.label}]" if t.label else ""
            lines.append(f"`#{t.id}` {format_duration(t.duration_seconds)} — {rem_str}{tag}")
        if len(timers) > 15:
            lines.append(f"... and {len(timers) - 15} more")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @timers_group.command(name="cancel", description="Cancel a timer by ID")
    @app_commands.describe(timer_id="The timer ID to cancel")
    async def timers_cancel_cmd(
        interaction: discord.Interaction,
        timer_id: int,
    ) -> None:
        from app.database import repository as repo
        from app.workers.scheduler import get_scheduler

        user = await repo.get_or_create_user(
            discord_user_id=str(interaction.user.id),
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        timer = await repo.get_timer(timer_id)
        if timer is None or timer.is_fired or timer.is_cancelled:
            await interaction.response.send_message(f"no active timer with id #{timer_id}", ephemeral=True)
            return

        if timer.user_id != user.id and not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message("you can only cancel your own timers", ephemeral=True)
            return

        ok = await repo.cancel_timer(timer_id)
        scheduler = get_scheduler()
        if scheduler is not None and ok:
            await scheduler.unschedule_timer(timer_id)

        if ok:
            await interaction.response.send_message(f"cancelled timer #{timer_id}", ephemeral=True)
        else:
            await interaction.response.send_message("couldn't cancel — it may have already fired", ephemeral=True)

    tree.add_command(timers_group)

        # ── /usage command ─────────────────────────────────────────
    @tree.command(name="usage", description="Check today's Groq API token usage")
    async def usage_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        import httpx
        from datetime import datetime, timezone

        settings = get_settings()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.groq.com/openai/v1/usage",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                    params={"date": today},
                )

            if resp.status_code != 200:
                await interaction.followup.send(
                    f"couldn't fetch usage (status {resp.status_code})", ephemeral=True
                )
                return

            data = resp.json()
            total_prompt = sum(m.get("prompt_tokens", 0) for m in data.get("data", []))
            total_completion = sum(m.get("completion_tokens", 0) for m in data.get("data", []))
            total = total_prompt + total_completion
            requests_count = sum(m.get("requests", 0) for m in data.get("data", []))

            # Per model breakdown
            model_lines = []
            for m in data.get("data", []):
                model_name = m.get("model_id", "unknown")
                tokens = m.get("prompt_tokens", 0) + m.get("completion_tokens", 0)
                reqs = m.get("requests", 0)
                if tokens > 0:
                    model_lines.append(f"  `{model_name}` — {tokens:,} tokens ({reqs} reqs)")

            breakdown = "\n".join(model_lines) if model_lines else "  no data"

            msg = (
                f"**Groq usage for {today}**\n"
                f"total tokens: **{total:,}**\n"
                f"requests: **{requests_count}**\n"
                f"prompt: {total_prompt:,} / completion: {total_completion:,}\n\n"
                f"**by model:**\n{breakdown}"
            )
            await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"error fetching usage: {e}", ephemeral=True)


async def sync_commands(
    tree: app_commands.CommandTree,
    client: discord.Client,
    guild: Optional[discord.Guild] = None,
) -> None:
    """Sync slash commands to Discord.

    If a guild is provided, syncs to that guild (instant).
    Otherwise syncs globally (can take up to 1 hour to propagate).
    """
    log.info("syncing_commands", guild_id=str(guild.id) if guild else "global")
    try:
        if guild is not None:
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
        else:
            synced = await tree.sync()
        log.info("commands_synced", count=len(synced), guild_id=str(guild.id) if guild else "global")
    except Exception as e:
        log.error("command_sync_failed", error=str(e))
