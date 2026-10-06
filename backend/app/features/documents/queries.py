"""Consultas de documentos. Todas exigen el propietario: no hay vía sin filtrar por usuario."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.features.documents.models import Document
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
