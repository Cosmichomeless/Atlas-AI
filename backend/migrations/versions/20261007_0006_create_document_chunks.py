"""create document chunks

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_document_chunks_ordinal_non_negative")),
        sa.CheckConstraint(
            "length(btrim(text)) > 0", name=op.f("ck_document_chunks_text_not_blank")
        ),
        sa.CheckConstraint(
            "page IS NULL OR page >= 1", name=op.f("ck_document_chunks_page_positive")
        ),
        sa.CheckConstraint(
            "(start_line IS NULL AND end_line IS NULL)"
            " OR (start_line IS NOT NULL AND end_line IS NOT NULL"
            " AND start_line >= 1 AND end_line >= start_line)",
            name=op.f("ck_document_chunks_lines_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "ordinal", name=op.f("uq_document_chunks_document_id")),
    )
    op.create_index(
        op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_document_chunks_document_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
