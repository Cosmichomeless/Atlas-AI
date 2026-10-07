"""document index fingerprint

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL = índice de origen desconocido: se considera obsoleto y se reindexa al pedirlo.
    op.add_column("documents", sa.Column("index_embedding", sa.String(length=400), nullable=True))
    op.add_column("documents", sa.Column("index_chunking", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "index_chunking")
    op.drop_column("documents", "index_embedding")
