"""document metadata and ingestion states

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("UPLOADED", "PROCESSING", "READY", "FAILED")


def upgrade() -> None:
    # Los documentos que ya existieran (solo con propietario) reciben valores provisionales
    # mediante un DEFAULT temporal que se retira al final: el esquema definitivo no los tiene.
    op.add_column(
        "documents",
        sa.Column("filename", sa.String(length=255), server_default="sin-nombre", nullable=False),
    )
    op.add_column(
        "documents",
        sa.Column(
            "content_type",
            sa.String(length=127),
            server_default="application/octet-stream",
            nullable=False,
        ),
    )
    op.add_column(
        "documents",
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "documents",
        sa.Column(
            "status",
            sa.Enum(
                *STATUSES,
                name="document_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="UPLOADED",
            nullable=False,
        ),
    )
    op.add_column("documents", sa.Column("error_summary", sa.Text(), nullable=True))
    op.add_column(
        "documents", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "documents",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "documents", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("documents", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "documents", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    for column in ("filename", "content_type", "size_bytes", "status"):
        op.alter_column("documents", column, server_default=None)
    op.create_check_constraint("size_non_negative", "documents", "size_bytes >= 0")
    op.create_check_constraint("attempts_non_negative", "documents", "attempts >= 0")
    op.create_index(op.f("ix_documents_status"), "documents", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_documents_status"), table_name="documents")
    op.drop_constraint(op.f("ck_documents_attempts_non_negative"), "documents", type_="check")
    op.drop_constraint(op.f("ck_documents_size_non_negative"), "documents", type_="check")
    for column in (
        "lease_expires_at",
        "processed_at",
        "processing_started_at",
        "updated_at",
        "attempts",
        "error_summary",
        "status",
        "size_bytes",
        "content_type",
        "filename",
    ):
        op.drop_column("documents", column)
