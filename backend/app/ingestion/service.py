"""Cola de ingestión respaldada por PostgreSQL: reservar, procesar y recuperar documentos.

La tabla `documents` es la cola. Un worker reserva el documento más antiguo pendiente con
`FOR UPDATE SKIP LOCKED` (varios workers no se pisan) y lo marca PROCESSING con un arrendamiento
(`lease_expires_at`). Si el proceso muere a mitad, nadie renueva el arrendamiento: al vencer, otro
worker recupera el documento y lo reintenta, hasta `INGESTION_MAX_ATTEMPTS` intentos.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.features.documents.extraction import extract_or_fail
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import FileStorage

logger = logging.getLogger(__name__)

INTERRUPTED = "El procesamiento se interrumpió y se superó el número máximo de intentos."
UNEXPECTED_RETRY = "Error inesperado durante el procesamiento; se reintentará."
UNEXPECTED_FAILURE = "Error inesperado durante el procesamiento; se superó el máximo de intentos."


def select_claimable(now: datetime) -> Select[Document]:
    """Documentos pendientes o con el arrendamiento vencido (worker caído), el más viejo primero."""
    return (
        select(Document)
        .where(
            or_(
                Document.status == DocumentStatus.UPLOADED,
                and_(
                    Document.status == DocumentStatus.PROCESSING,
                    or_(Document.lease_expires_at.is_(None), Document.lease_expires_at < now),
                ),
            )
        )
        .order_by(Document.created_at, Document.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def claim_next(
    session: Session, *, lease_seconds: int, max_attempts: int, now: datetime | None = None
) -> Document | None:
    """Reserva el siguiente documento y confirma la reserva; `None` si no hay trabajo."""
    now = now or datetime.now(UTC)
    while True:
        document = session.scalars(select_claimable(now)).first()
        if document is None:
            session.rollback()
            return None

        if document.status is DocumentStatus.PROCESSING:
            # Su worker murió sin terminar: vuelve a la cola o, si ya agotó los intentos, falla.
            if document.attempts >= max_attempts:
                document.transition_to(DocumentStatus.FAILED, error_summary=INTERRUPTED, now=now)
                session.commit()
                logger.warning("Documento %s agotó sus intentos tras una caída", document.id)
                continue
            document.transition_to(DocumentStatus.UPLOADED, now=now)

        document.transition_to(
            DocumentStatus.PROCESSING,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            now=now,
        )
        session.commit()
        return document


def process(
    session: Session, storage: FileStorage, document: Document, *, max_attempts: int
) -> None:
    """Procesa un documento reservado y confirma su estado final.

    Un fallo del propio documento (sin texto, dañado…) lo deja FAILED con su causa. Un error
    inesperado lo devuelve a la cola para reintentar, o lo da por FAILED al agotar los intentos.
    """
    try:
        blocks = extract_or_fail(document, storage)
        if document.status is DocumentStatus.PROCESSING:
            # Aquí se encadenarán las etapas siguientes (fragmentación, embeddings) antes de READY.
            logger.info("Documento %s: %d bloques extraídos", document.id, len(blocks))
            document.transition_to(DocumentStatus.READY)
        session.commit()
    except Exception:
        logger.exception("Fallo inesperado procesando el documento %s", document.id)
        session.rollback()
        session.refresh(document)
        if document.attempts >= max_attempts:
            document.transition_to(DocumentStatus.FAILED, error_summary=UNEXPECTED_FAILURE)
        else:
            document.transition_to(DocumentStatus.UPLOADED)
            document.error_summary = UNEXPECTED_RETRY
        session.commit()


def run_once(
    session: Session,
    storage: FileStorage,
    *,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> bool:
    """Reserva y procesa un documento; devuelve si había trabajo."""
    document = claim_next(session, lease_seconds=lease_seconds, max_attempts=max_attempts, now=now)
    if document is None:
        return False
    process(session, storage, document, max_attempts=max_attempts)
    return True
