"""phase 2: summaries, memories, style_samples, style_profiles

Revision ID: 0002_phase2
Revises: 0001_initial
Create Date: 2025-02-01 00:00:00

Adds Phase 2 tables for memory + style learning:
- conversation_summaries : per-channel rolling summary
- memories               : admin-approved long-term structured memory
- style_samples           : admin-approved message samples (source for style profile)
- style_profiles          : derived per-channel style profile (one row per channel)

All tables designed to run unchanged on SQLite (dev) and Postgres (prod).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_phase2"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── conversation_summaries ────────────────────────────────
    op.create_table(
        "conversation_summaries",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("message_range_start", sa.String(32), nullable=False),
        sa.Column("message_range_end", sa.String(32), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_summaries_channel_active",
        "conversation_summaries",
        ["channel_id", "is_active"],
    )

    # ── memories ───────────────────────────────────────────────
    op.create_table(
        "memories",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), sa.ForeignKey("channels.id"), nullable=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(32), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_memories_channel_kind",
        "memories",
        ["channel_id", "kind"],
    )

    # ── style_samples ─────────────────────────────────────────
    op.create_table(
        "style_samples",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column(
            "source_message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id"),
            nullable=False,
        ),
        sa.Column(
            "approved_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("tags", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("is_used_in_profile", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_style_samples_channel",
        "style_samples",
        ["channel_id"],
    )

    # ── style_profiles ────────────────────────────────────────
    op.create_table(
        "style_profiles",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "channel_id",
            sa.BigInteger(),
            sa.ForeignKey("channels.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("profile_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_sample_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("style_profiles")
    op.drop_index("idx_style_samples_channel", table_name="style_samples")
    op.drop_table("style_samples")
    op.drop_index("idx_memories_channel_kind", table_name="memories")
    op.drop_table("memories")
    op.drop_index("idx_summaries_channel_active", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")
