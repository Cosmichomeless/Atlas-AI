"""Borrado de un documento y de todo lo que se derivó de él."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import FileStorage


class DocumentBusyError(Exception):
    """Un worker lo está procesando ahora mismo; borrarlo ahora le arrebataría el documento."""


def delete_owned(
    session: Session,
    storage: FileStorage,
    owner_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Borra el documento del usuario con su archivo, fragmentos y vectores; confirma la operación.

    Devuelve `False` si no existe o es de otro usuario (no se distingue). Lanza `DocumentBusyError`
    mientras un worker tiene el documento con el arrendamiento vigente; un `PROCESSING` con el
    arrendamiento vencido (worker caído) sí se puede borrar.

    El archivo se retira primero: si falla, el documento sigue ahí y se puede reintentar; nunca
    queda un archivo huérfano e inalcanzable. Los fragmentos y vectores desaparecen con la fila
    (`ON DELETE CASCADE`). La fila se bloquea para no competir con un worker que la reserva.
    """
    document = session.scalars(
        select(Document)
        .where(Document.id == document_id, Document.owner_id == owner_id)
        .with_for_update()
    ).first()
    if document is None:
        return False
    now = now or datetime.now(UTC)
    if (
        document.status is DocumentStatus.PROCESSING
        and document.lease_expires_at is not None
        and document.lease_expires_at > now
    ):
        session.rollback()
        raise DocumentBusyError(str(document_id))

    storage.delete(document.storage_key)
    session.delete(document)
    session.commit()
    return True
