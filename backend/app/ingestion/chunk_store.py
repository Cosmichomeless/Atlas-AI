"""Persistencia de fragmentos: reemplazar los de un documento de una vez, sin duplicados."""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.features.documents.models import DocumentChunk
from app.ingestion.chunking import Chunk


def replace_chunks(session: Session, document_id: uuid.UUID, chunks: Sequence[Chunk]) -> None:
    """Sustituye todos los fragmentos del documento por `chunks` dentro de la transacción actual.

    Al reprocesar, los fragmentos viejos desaparecen y los nuevos ocupan su lugar; si la
    transacción falla, los viejos siguen intactos. El llamador confirma.
    """
    session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    session.add_all(
        DocumentChunk(
            document_id=document_id,
            ordinal=chunk.ordinal,
            text=chunk.text,
            page=chunk.page,
            section=chunk.section,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
        )
        for chunk in chunks
    )
    session.flush()


def list_chunks(session: Session, document_id: uuid.UUID) -> Sequence[DocumentChunk]:
    """Fragmentos de un documento en el orden del original."""
    return session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.ordinal)
    ).all()
