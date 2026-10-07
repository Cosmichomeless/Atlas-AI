"""Consultas de documentos. Todas exigen el propietario: no hay vía sin filtrar por usuario."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus


def owned_documents(owner_id: uuid.UUID) -> Select[Document]:
    """Base de cualquier consulta de documentos: solo los del usuario indicado."""
    return select(Document).where(Document.owner_id == owner_id)


def list_owned(session: Session, owner_id: uuid.UUID) -> Sequence[Document]:
    return session.scalars(owned_documents(owner_id).order_by(Document.created_at.desc())).all()


def list_owned_page(
    session: Session,
    owner_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    status: DocumentStatus | None = None,
) -> tuple[Sequence[Document], int]:
    """Página de documentos del usuario (más recientes primero) y el total que cumple el filtro."""
    base = owned_documents(owner_id)
    if status is not None:
        base = base.where(Document.status == status)
    total = session.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    # `id` desempata para que la paginación sea estable con fechas idénticas.
    page = base.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).offset(offset)
    return session.scalars(page).all(), total


def get_owned(session: Session, owner_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
    """Devuelve `None` tanto si no existe como si pertenece a otro usuario (no revela cuál)."""
    return session.scalars(owned_documents(owner_id).where(Document.id == document_id)).first()


def get_owned_chunk(
    session: Session, owner_id: uuid.UUID, document_id: uuid.UUID, chunk_id: uuid.UUID
) -> tuple[Document, DocumentChunk] | None:
    """Fragmento con su documento si ambos existen, encajan entre sí y son del usuario.

    Devuelve `None` en cualquier otro caso (inexistente, de otro documento o ajeno) sin distinguir.
    """
    row = session.execute(
        owned_documents(owner_id)
        .add_columns(DocumentChunk)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(Document.id == document_id, DocumentChunk.id == chunk_id)
    ).first()
    return (row[0], row[1]) if row is not None else None
