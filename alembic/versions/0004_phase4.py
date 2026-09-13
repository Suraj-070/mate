"""phase 4: add preferred_language column to users

Revision ID: 0004_phase4
Revises: 0003_phase3
Create Date: 2025-04-01 00:00:00

Adds Phase 4 changes:
- users.preferred_language : nullable, one of 'en' | 'ne-deva' | 'ne-roman' | 'mixed'

No new tables. Memory extraction proposals are stored transiently (they're
posted as ephemeral Discord messages, not in DB) — if a proposal is approved
via reaction, it becomes a regular `memories` row.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_phase4"
down_revision: Union[str, None] = "0003_phase3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite ALTER TABLE workaround (render_as_batch) is handled in env.py
    op.add_column(
        "users",
        sa.Column(
            "preferred_language",
            sa.String(16),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # SQLite ALTER TABLE workaround
    op.drop_column("users", "preferred_language")
