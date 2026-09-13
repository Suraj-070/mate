"""initial schema: channels, users, messages

Revision ID: 0001_initial
Revises:
Create Date: 2025-01-01 00:00:00

Tables created:
- channels  : per-channel enable/disable + config
- users     : per-user info + privacy opt-out flags
- messages  : message log for audit + buffer rebuild + style sampling source

Designed to run unchanged on SQLite (dev) and Postgres (prod).
On SQLite we use INTEGER PRIMARY KEY AUTOINCREMENT.
On Postgres we use BIGSERIAL via server_default.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Detect dialect for portable column types
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # ── channels ───────────────────────────────────────────────
    op.create_table(
        "channels",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("discord_guild_id", sa.String(32), nullable=False),
        sa.Column("discord_channel_id", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("config_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
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

    # ── users ──────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("discord_user_id", sa.String(32), nullable=False, unique=True),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("opt_out_style", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("opt_out_memory", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("opt_out_reply", sa.Boolean(), nullable=False, server_default=sa.text("0")),
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

    # ── messages ──────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("discord_message_id", sa.String(32), nullable=False),
        sa.Column("author_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("reply_to_message_id", sa.String(32), nullable=True),
        sa.Column("language_hint", sa.String(16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("channel_id", "discord_message_id", name="uq_messages_channel_discord"),
    )
    op.create_index(
        "idx_messages_channel_created",
        "messages",
        ["channel_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_messages_channel_created", table_name="messages")
    op.drop_table("messages")
    op.drop_table("users")
    op.drop_table("channels")
