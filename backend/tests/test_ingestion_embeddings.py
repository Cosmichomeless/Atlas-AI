"""Indexación durante la ingesta: READY solo con todos los fragmentos indexados, sin duplicados."""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.models import ChunkEmbedding
from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.ingestion import service
from app.ingestion.chunk_store import list_chunks
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.service import run_once
from tests.pdfs import make_pdf
from tests.test_chunk_persistence import SMALL, requeue, text_document
from tests.test_ingestion import EMBEDDER, LEASE, MAX_ATTEMPTS, T0, add_document

TEXT = " ".join(f"palabra{n}" for n in range(150))


class CountingProvider(FakeEmbeddingProvider):
    """Falso que cuenta las llamadas y falla a partir de la `fail_from`-ésima tanda."""

    max_batch_size = 2

    def __init__(self, dimensions: int = 1536, fail_from: int | None = None) -> None:
        super().__init__("fake-model", dimensions)
        self.calls = 0
        self.fail_from = fail_from

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise EmbeddingError("el servicio de embeddings no responde")
        return super()._embed_batch(texts)


def ingest(
    session: Session,
    storage: LocalFileStorage,
    embedder: EmbeddingProvider = EMBEDDER,
    policy: ChunkPolicy = SMALL,
    max_attempts: int = MAX_ATTEMPTS,
) -> None:
    run_once(
        session,
        storage,
        policy=policy,
        embedder=embedder,
        lease_seconds=LEASE,
        max_attempts=max_attempts,
        now=T0,
    )


def vectors(session: Session, document: Document) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(ChunkEmbedding)
            .join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
            .where(DocumentChunk.document_id == document.id)
        )
        or 0
    )


def chunks(session: Session, document: Document) -> int:
    return len(list_chunks(session, document.id))


# ── Documento listo ─────────────────────────────────────────────────────────


def test_a_ready_document_has_every_chunk_indexed(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)

    ingest(db_session, storage)

    assert document.status is DocumentStatus.READY
    assert chunks(db_session, document) > 2
    assert vectors(db_session, document) == chunks(db_session, document)
    specs = {
        (row.provider, row.model, row.dimensions, row.version)
        for row in db_session.scalars(select(ChunkEmbedding))
    }
    assert specs == {("fake", "fake-model", 1536, EMBEDDER.spec.version)}


def test_the_text_is_embedded_in_batches(db_session: Session, storage: LocalFileStorage) -> None:
    text_document(db_session, storage, TEXT)
    provider = CountingProvider()

    ingest(db_session, storage, provider)

    assert provider.calls > 1


def test_reindexing_a_document_never_duplicates_vectors(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    ingest(db_session, storage)
    first = vectors(db_session, document)

    requeue(document)
    ingest(db_session, storage)

    assert document.status is DocumentStatus.READY
    assert vectors(db_session, document) == first == chunks(db_session, document)


def test_reindexing_with_another_policy_leaves_only_the_new_vectors(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    ingest(db_session, storage)

    requeue(document)
    ingest(db_session, storage, policy=ChunkPolicy(size=400, overlap=50))

    assert chunks(db_session, document) > 0
    assert vectors(db_session, document) == chunks(db_session, document)


def test_a_document_without_text_is_not_sent_to_the_provider(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = add_document(db_session, storage, make_pdf([None]))
    provider = CountingProvider()

    ingest(db_session, storage, provider)

    assert document.status is DocumentStatus.FAILED
    assert provider.calls == 0
    assert vectors(db_session, document) == 0


# ── Fallos parciales ────────────────────────────────────────────────────────


def test_a_partial_failure_leaves_no_vectors_and_the_document_is_retried(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)

    ingest(db_session, storage, CountingProvider(fail_from=2))

    assert document.status is DocumentStatus.UPLOADED
    assert document.error_summary == service.EMBEDDING_RETRY
    assert vectors(db_session, document) == 0
    assert chunks(db_session, document) == 0


def test_the_retry_completes_the_indexing_without_duplicates(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    ingest(db_session, storage, CountingProvider(fail_from=2))

    ingest(db_session, storage)

    assert document.status is DocumentStatus.READY
    assert document.error_summary is None
    assert vectors(db_session, document) == chunks(db_session, document) > 0


def test_a_document_fails_for_good_when_embeddings_keep_failing(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)

    for _ in range(MAX_ATTEMPTS):
        ingest(db_session, storage, CountingProvider(fail_from=1))

    assert document.status is DocumentStatus.FAILED
    assert document.error_summary == service.EMBEDDING_FAILURE
    assert vectors(db_session, document) == 0


def test_a_failed_reindex_keeps_the_previous_chunks_and_vectors(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    ingest(db_session, storage)
    before = vectors(db_session, document)

    requeue(document)
    ingest(db_session, storage, CountingProvider(fail_from=2), ChunkPolicy(size=400, overlap=50))

    assert document.status is DocumentStatus.UPLOADED
    assert vectors(db_session, document) == before == chunks(db_session, document)


def test_the_error_summary_does_not_leak_the_provider_message(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)

    ingest(db_session, storage, CountingProvider(fail_from=1))

    assert document.error_summary is not None
    assert "no responde" not in document.error_summary


# ── Dimensión incompatible ──────────────────────────────────────────────────


def test_an_incompatible_dimension_fails_clearly_without_retrying(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    provider = CountingProvider(dimensions=8)

    ingest(db_session, storage, provider)

    assert document.status is DocumentStatus.FAILED
    assert document.error_summary == service.INCOMPATIBLE_DIMENSIONS
    assert "EMBEDDING_DIMENSIONS" in document.error_summary
    assert document.attempts == 1
    assert provider.calls == 0
    assert vectors(db_session, document) == 0
