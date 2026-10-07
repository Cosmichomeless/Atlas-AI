"""Cola de ingestión respaldada por PostgreSQL: reservar, procesar y recuperar documentos.

La tabla `documents` es la cola. Un worker reserva el documento más antiguo pendiente con
`FOR UPDATE SKIP LOCKED` (varios workers no se pisan) y lo marca PROCESSING con un arrendamiento
(`lease_expires_at`). Si el proceso muere a mitad, nadie renueva el arrendamiento: al vencer, otro
worker recupera el documento y lo reintenta, hasta `INGESTION_MAX_ATTEMPTS` intentos.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.embeddings.store import IncompatibleDimensionsError, ensure_fits_schema, save_embeddings
from app.features.documents.extraction import extract_or_fail
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import FileStorage
from app.ingestion.chunk_store import list_chunks, replace_chunks
from app.ingestion.chunking import ChunkPolicy, chunk_blocks

logger = logging.getLogger(__name__)

INTERRUPTED = "El procesamiento se interrumpió y se superó el número máximo de intentos."
NO_TEXT = "El documento no contiene texto extraíble."
UNEXPECTED_RETRY = "Error inesperado durante el procesamiento; se reintentará."
UNEXPECTED_FAILURE = "Error inesperado durante el procesamiento; se superó el máximo de intentos."
EMBEDDING_RETRY = "No se pudieron generar los embeddings; se reintentará."
EMBEDDING_FAILURE = "No se pudieron generar los embeddings; se superó el máximo de intentos."
INCOMPATIBLE_DIMENSIONS = (
    "La dimensión de los embeddings configurados no cabe en el esquema de vectores; "
    "revisa EMBEDDING_DIMENSIONS."
)


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
    session: Session,
    storage: FileStorage,
    document: Document,
    *,
    policy: ChunkPolicy,
    embedder: EmbeddingProvider,
    max_attempts: int,
) -> None:
    """Procesa un documento reservado y confirma su estado final.

    Extrae, fragmenta e indexa (embeddings) en una sola transacción: el documento llega a READY
    con todos sus fragmentos indexados o no cambia nada. Un fallo del propio documento (sin texto,
    dañado…) lo deja FAILED con su causa. Un fallo del proveedor de embeddings o un error
    inesperado lo devuelve a la cola para reintentar, o lo da por FAILED al agotar los intentos,
    sin dejar vectores a medias. Una dimensión incompatible con el esquema es de configuración:
    reintentar no ayuda, así que falla de inmediato.
    """
    try:
        blocks = extract_or_fail(document, storage)
        chunks = (
            chunk_blocks(blocks, policy) if document.status is DocumentStatus.PROCESSING else []
        )
        # Sustituye los fragmentos viejos en la misma transacción que el estado final: si el
        # documento ya no da texto no se queda con restos del contenido anterior.
        replace_chunks(session, document.id, chunks)
        if document.status is DocumentStatus.PROCESSING:
            if chunks:
                _index_chunks(session, document, embedder)
                logger.info("Documento %s: %d fragmentos indexados", document.id, len(chunks))
                document.transition_to(
                    DocumentStatus.READY,
                    index_embedding=embedder.spec.key,
                    index_chunking=policy.key,
                )
            else:
                document.transition_to(DocumentStatus.FAILED, error_summary=NO_TEXT)
        session.commit()
    except IncompatibleDimensionsError:
        logger.exception("Dimensión de embeddings incompatible con el esquema (%s)", document.id)
        session.rollback()
        session.refresh(document)
        document.transition_to(DocumentStatus.FAILED, error_summary=INCOMPATIBLE_DIMENSIONS)
        session.commit()
    except EmbeddingError:
        logger.exception("Fallo de embeddings procesando el documento %s", document.id)
        _retry_or_fail(session, document, max_attempts, EMBEDDING_RETRY, EMBEDDING_FAILURE)
    except Exception:
        logger.exception("Fallo inesperado procesando el documento %s", document.id)
        _retry_or_fail(session, document, max_attempts, UNEXPECTED_RETRY, UNEXPECTED_FAILURE)


def _index_chunks(session: Session, document: Document, embedder: EmbeddingProvider) -> None:
    """Genera y guarda los vectores de los fragmentos recién escritos (sin confirmar)."""
    ensure_fits_schema(embedder.spec)
    chunks = list_chunks(session, document.id)
    save_embeddings(session, chunks, embedder.embed([chunk.text for chunk in chunks]))


def _retry_or_fail(
    session: Session, document: Document, max_attempts: int, retry: str, failure: str
) -> None:
    """Deshace la transacción y devuelve el documento a la cola, o lo da por FAILED."""
    session.rollback()
    try:
        session.refresh(document)
    except InvalidRequestError:
        logger.warning("El documento %s se borró mientras se procesaba", document.id)
        return
    if document.attempts >= max_attempts:
        document.transition_to(DocumentStatus.FAILED, error_summary=failure)
    else:
        document.transition_to(DocumentStatus.UPLOADED)
        document.error_summary = retry
    session.commit()


def run_once(
    session: Session,
    storage: FileStorage,
    *,
    policy: ChunkPolicy,
    embedder: EmbeddingProvider,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> bool:
    """Reserva y procesa un documento; devuelve si había trabajo."""
    document = claim_next(session, lease_seconds=lease_seconds, max_attempts=max_attempts, now=now)
    if document is None:
        return False
    process(session, storage, document, policy=policy, embedder=embedder, max_attempts=max_attempts)
    return True
