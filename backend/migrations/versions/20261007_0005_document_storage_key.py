"""document storage key

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("storage_key", sa.String(length=512), nullable=True))
    # Misma clave que genera la aplicación: <propietario>/<documento>/original
    op.execute(
        "UPDATE documents SET storage_key = owner_id::text || '/' || id::text || '/original'"
    )
    op.alter_column("documents", "storage_key", nullable=False)
    op.create_unique_constraint(op.f("uq_documents_storage_key"), "documents", ["storage_key"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_documents_storage_key"), "documents", type_="unique")
    op.drop_column("documents", "storage_key")
