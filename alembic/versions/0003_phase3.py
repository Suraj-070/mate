"""phase 3: reminders, timers

Revision ID: 0003_phase3
Revises: 0002_phase2
Create Date: 2025-03-01 00:00:00

Adds Phase 3 tables for the tool framework:
- reminders : persisted reminders, fired by APScheduler
- timers     : short-duration timers, fired by APScheduler

Both tables persist jobs so the bot can recover on restart — the scheduler
reads all unfired + uncancelled rows and re-registers them with APScheduler.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_phase3"
down_revision: Union[str, None] = "0002_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── reminders ──────────────────────────────────────────────
    op.create_table(
        "reminders",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence", sa.String(64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_fired", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_reminders_pending",
        "reminders",
        ["trigger_at"],
    )

    # ── timers ────────────────────────────────────────────────
    op.create_table(
        "timers",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("label", sa.String(256), nullable=True),
        sa.Column("is_fired", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_timers_pending",
        "timers",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_timers_pending", table_name="timers")
    op.drop_table("timers")
    op.drop_index("idx_reminders_pending", table_name="reminders")
    op.drop_table("reminders")
