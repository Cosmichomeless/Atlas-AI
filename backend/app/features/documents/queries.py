"""Consultas de documentos. Todas exigen el propietario: no hay vía sin filtrar por usuario."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.features.documents.models import Document


def owned_documents(owner_id: uuid.UUID) -> Select[Document]:
    """Base de cualquier consulta de documentos: solo los del usuario indicado."""
    return select(Document).where(Document.owner_id == owner_id)


def list_owned(session: Session, owner_id: uuid.UUID) -> Sequence[Document]:
    return session.scalars(owned_documents(owner_id).order_by(Document.created_at.desc())).all()


def get_owned(session: Session, owner_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
    """Devuelve `None` tanto si no existe como si pertenece a otro usuario (no revela cuál)."""
    return session.scalars(owned_documents(owner_id).where(Document.id == document_id)).first()
