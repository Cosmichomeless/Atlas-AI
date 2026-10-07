"""Reindexación: `python -m app.ingestion.reindex [--dry-run]` (desde `backend/`).

Cada documento READY recuerda con qué se indexó (`index_embedding` = `EmbeddingSpec.key`,
`index_chunking` = `ChunkPolicy.key`). Si cambia el modelo de embeddings, su versión o la política
de fragmentación, esa huella deja de coincidir con la configuración vigente y el documento queda
**obsoleto**; también los READY anteriores a la huella (NULL), cuyo origen se desconoce.

Reindexar no toca los vectores: devuelve el documento a la cola (READY → UPLOADED) y el worker lo
reprocesa con la configuración actual. Ahí `replace_chunks` cambia fragmentos y vectores viejos por
los nuevos en una sola transacción, así que un documento tiene siempre un índice completo y de una
única versión: nunca se mezclan, y repetir el proceso no duplica nada. La recuperación solo compara
contra vectores de la especificación vigente, por lo que durante una migración parcial los
documentos ya migrados responden con el modelo nuevo y el resto espera su turno.

Es idempotente: tras pedirla, los documentos ya encolados dejan de ser READY y una segunda llamada
no encuentra nada que hacer.
"""

import argparse
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.embeddings.provider import EmbeddingSpec
from app.embeddings.registry import get_embedding_provider
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.ingestion.chunking import ChunkPolicy

logger = logging.getLogger("atlas.reindex")

BATCH_SIZE = 500


@dataclass(frozen=True)
class IndexCoverage:
    """Cuántos documentos hay en cada situación respecto al índice vigente."""

    current: int  # READY indexados con la configuración vigente
    stale: int  # READY indexados con otra configuración (o de origen desconocido)
    pending: int  # UPLOADED o PROCESSING: ya están en la cola
    failed: int  # FAILED: no se tocan, tienen su propio reintento

    @property
    def total(self) -> int:
        return self.current + self.stale + self.pending + self.failed


def _is_stale(policy: ChunkPolicy, spec: EmbeddingSpec) -> ColumnElement[bool]:
    # IS DISTINCT FROM trata NULL como "distinto": un READY sin huella es obsoleto.
    return or_(
        Document.index_embedding.is_distinct_from(spec.key),
        Document.index_chunking.is_distinct_from(policy.key),
    )


def index_coverage(
    session: Session,
    *,
    policy: ChunkPolicy,
    spec: EmbeddingSpec,
    owner_id: uuid.UUID | None = None,
) -> IndexCoverage:
    """Cuenta los documentos por situación sin modificar nada."""
    stale = _is_stale(policy, spec)
    ready = Document.status == DocumentStatus.READY
    queued = Document.status.in_((DocumentStatus.UPLOADED, DocumentStatus.PROCESSING))
    query = select(
        func.count().filter(ready, ~stale),
        func.count().filter(ready, stale),
        func.count().filter(queued),
        func.count().filter(Document.status == DocumentStatus.FAILED),
    )
    if owner_id is not None:
        query = query.where(Document.owner_id == owner_id)
    current, obsolete, pending, failed = session.execute(query).one()
    return IndexCoverage(current=current, stale=obsolete, pending=pending, failed=failed)


def request_reindex(
    session: Session,
    *,
    policy: ChunkPolicy,
    spec: EmbeddingSpec,
    owner_id: uuid.UUID | None = None,
) -> int:
    """Encola los documentos READY obsoletos y devuelve cuántos; repetirla devuelve 0.

    Reinicia los intentos (son de la pasada anterior) y limpia el error. Cada lote se confirma
    aparte; los documentos que otro proceso tenga bloqueado (p. ej. se están borrando) se saltan
    y entran en la siguiente llamada.
    """
    total = 0
    while True:
        query = (
            select(Document)
            .where(Document.status == DocumentStatus.READY, _is_stale(policy, spec))
            .order_by(Document.created_at, Document.id)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        if owner_id is not None:
            query = query.where(Document.owner_id == owner_id)
        documents = session.scalars(query).all()
        if not documents:
            session.rollback()
            return total
        for document in documents:
            document.transition_to(DocumentStatus.UPLOADED)
            document.attempts = 0
            document.error_summary = None
        session.commit()
        total += len(documents)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Encola para reindexar los documentos indexados con otro modelo o política."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="solo muestra el estado; no encola nada"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    policy = ChunkPolicy.from_settings(get_settings())
    spec = get_embedding_provider().spec
    with get_sessionmaker()() as session:
        coverage = index_coverage(session, policy=policy, spec=spec)
        logger.info(
            "Índice vigente %s con política %s: %d al día, %d obsoletos, %d en cola, %d fallidos",
            spec.key,
            policy.key,
            coverage.current,
            coverage.stale,
            coverage.pending,
            coverage.failed,
        )
        if args.dry_run:
            return
        queued = request_reindex(session, policy=policy, spec=spec)
        logger.info("%d documentos encolados para reindexar; el worker los procesará", queued)


if __name__ == "__main__":
    main()
