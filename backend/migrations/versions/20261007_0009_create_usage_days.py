"""create usage days

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_days",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("questions", sa.Integer(), nullable=False),
        sa.Column("embedding_calls", sa.Integer(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "questions >= 0 AND embedding_calls >= 0 AND llm_calls >= 0 "
            "AND input_tokens >= 0 AND output_tokens >= 0",
            name=op.f("ck_usage_days_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_usage_days_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_id", "day", name=op.f("pk_usage_days")),
    )


def downgrade() -> None:
    op.drop_table("usage_days")
