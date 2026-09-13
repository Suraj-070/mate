"""Application configuration.

Single source of truth for all runtime settings. Loaded from environment
variables (or .env file) via pydantic-settings. No hardcoded secrets.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application settings. Override any via env var or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Discord ────────────────────────────────────────────────
    discord_bot_token: str = Field(description="Bot token from Discord Developer Portal")
    discord_app_id: str = Field(description="Application (client) ID — used for mention detection")
    discord_admin_ids: str = Field(
        default="",
        description="Comma-separated Discord user IDs of admins",
    )
    discord_test_guild_id: str = Field(
        default="",
        description="Test guild ID — slash commands registered here only during dev",
    )

    # ── LLM Provider (Groq) ───────────────────────────────────
    groq_api_key: str = Field(description="API key from console.groq.com")
    groq_model: str = Field(
        default="llama-3.3-70b-versatile",
        description="Groq model name — see https://console.groq.com/docs/models",
    )
    llm_max_tokens: int = Field(default=400, ge=50, le=4000)
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # ── Database ───────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/groupmate.db",
        description="SQLAlchemy async URL. SQLite for dev, postgresql+asyncpg:// for prod",
    )

    # ── Conversation tuning ───────────────────────────────────
    context_buffer_size: int = Field(default=20, ge=4, le=100)
    context_max_tokens: int = Field(default=2000, ge=200, le=32000)
    reply_cooldown_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    max_replies_per_minute: int = Field(default=10, ge=1, le=60)

    # ── Logging ───────────────────────────────────────────────
    log_level: str = Field(default="INFO")

    # ── Feature flags (Phase 2) ────────────────────────────────
    enable_style_learning: bool = Field(
        default=True,
        description="Enable admin-approved style sampling + per-channel style profile",
    )
    enable_long_term_memory: bool = Field(
        default=True,
        description="Enable admin-approved long-term memory (slash commands + context menu)",
    )
    enable_conversation_summaries: bool = Field(
        default=True,
        description="Periodically summarize older buffer messages per channel",
    )
    enable_autonomous_participation: bool = Field(
        default=False,
        description="Allow bot to chime in without being explicitly mentioned",
    )
    enable_react_outcome: bool = Field(
        default=True,
        description="Allow bot to emoji-react to messages it has nothing to say to",
    )
    enable_llm_decision_assist: bool = Field(
        default=False,
        description="Use an LLM call to decide gray-zone autonomous participation. Adds latency + cost.",
    )

    # ── Memory tuning ─────────────────────────────────────────
    summary_trigger_message_count: int = Field(
        default=30, ge=10, le=200,
        description="Summarize when channel buffer reaches this many unsummarized messages",
    )
    max_memories_per_channel: int = Field(
        default=50, ge=5, le=500,
        description="Max long-term memories returned per channel for context",
    )
    memory_expiry_sweep_minutes: int = Field(
        default=60, ge=5, le=1440,
        description="How often to sweep expired memories (minutes)",
    )

    # ── Style tuning ──────────────────────────────────────────
    style_profile_rebuild_threshold: int = Field(
        default=5, ge=1, le=50,
        description="Rebuild style profile when N new approved samples are added",
    )

    # ── Tools (Phase 3) ───────────────────────────────────────
    enable_tools: bool = Field(
        default=True,
        description="Enable tool-calling framework (reminders, timers, custom APIs)",
    )
    enable_reminders: bool = Field(
        default=True,
        description="Allow LLM to create/list/cancel reminders",
    )
    enable_timers: bool = Field(
        default=True,
        description="Allow LLM to start/list/cancel timers",
    )
    tool_max_iterations: int = Field(
        default=3, ge=1, le=10,
        description="Max LLM↔tool round-trips per message (prevents loops)",
    )

    # ── Scheduler (Phase 3) ───────────────────────────────────
    user_timezone: str = Field(
        default="Australia/Sydney",
        description="IANA timezone for natural-language time parsing + display",
    )
    scheduler_job_interval: float = Field(
        default=5.0, ge=1.0, le=60.0,
        description="APScheduler misfire_grace_sec + check interval",
    )

    # ── Phase 4: Custom API tools ─────────────────────────────
    enable_custom_api_tools: bool = Field(
        default=True,
        description="Enable custom HTTP-based tools (weather, etc.)",
    )
    http_tool_timeout_seconds: float = Field(
        default=8.0, ge=1.0, le=30.0,
        description="Per-request timeout for HTTP tools",
    )
    http_tool_max_response_bytes: int = Field(
        default=16384, ge=512, le=65536,
        description="Max response body size for HTTP tools (truncated beyond this)",
    )

    # ── Phase 4: Memory extraction ───────────────────────────
    enable_memory_extraction: bool = Field(
        default=True,
        description="LLM proposes memories from chat; admins approve via ✅ reaction",
    )
    memory_extraction_trigger_message_count: int = Field(
        default=15, ge=5, le=100,
        description="Run extraction every N messages per channel",
    )
    memory_extraction_max_proposals_per_run: int = Field(
        default=3, ge=1, le=10,
        description="Cap proposals per extraction run (avoid spamming admins)",
    )

    # ── Phase 4: Topic tracking ──────────────────────────────
    enable_topic_tracking: bool = Field(
        default=True,
        description="Detect active topic per channel and inject into context",
    )
    topic_keywords_count: int = Field(
        default=5, ge=2, le=10,
        description="How many top keywords to extract per channel",
    )

    # ── Phase 4: Per-user language preferences ───────────────
    enable_per_user_language: bool = Field(
        default=True,
        description="Allow users to set a preferred language for replies",
    )

    # ── Derived helpers ───────────────────────────────────────
    @property
    def admin_ids(self) -> List[int]:
        """Parse DISCORD_ADMIN_IDS into a list of ints. Empty list if unset."""
        if not self.discord_admin_ids:
            return []
        return [
            int(x.strip())
            for x in self.discord_admin_ids.split(",")
            if x.strip().isdigit()
        ]

    @field_validator("discord_bot_token", "groq_api_key")
    @classmethod
    def _no_placeholder(cls, v: str) -> str:
        if v == "replace_me" or not v:
            raise ValueError(
                "Required secret not set. Copy .env.example to .env and fill in real values."
            )
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Use this everywhere — never construct Settings() directly."""
    return Settings()  # type: ignore[call-arg]