"""Reindexación: detectar índices obsoletos y rehacerlos sin mezclar versiones ni duplicar."""

import io
import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.models import ChunkEmbedding
from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.embeddings.store import nearest_chunks
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.ingestion import reindex
from app.ingestion.chunk_store import list_chunks
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.reindex import index_coverage, request_reindex
from app.ingestion.service import EMBEDDING_RETRY, claim_next, process
from tests.factories import make_document
from tests.test_chunk_persistence import SMALL
from tests.test_ingestion import EMBEDDER, MAX_ATTEMPTS, add_document, add_user
from tests.test_ingestion_embeddings import TEXT, ingest

OLD = EMBEDDER
BIG = ChunkPolicy(size=300, overlap=30)


class NewModel(FakeEmbeddingProvider):
    """Otro modelo de embeddings (otra spec); puede simular una caída del servicio."""

    def __init__(
        self, model: str = "fake-model-v2", *, version: str | None = None, down: bool = False
    ) -> None:
        super().__init__(model, 1536)
        if version is not None:
            self.spec = replace(self.spec, version=version)
        self.down = down

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if self.down:
            raise EmbeddingError("el servicio de embeddings no responde")
        return super()._embed_batch(texts)


NEW = NewModel()


def owned_document(
    session: Session, storage: LocalFileStorage, owner_id: uuid.UUID, content: str = TEXT
) -> Document:
    document = make_document(
        owner_id, filename="nota.txt", content_type="text/plain", size_bytes=len(content)
    )
    session.add(document)
    session.flush()
    storage.save(document.storage_key, io.BytesIO(content.encode()))
    return document


def indexed(
    session: Session,
    storage: LocalFileStorage,
    embedder: EmbeddingProvider = OLD,
    policy: ChunkPolicy = SMALL,
    owner_id: uuid.UUID | None = None,
    content: str = TEXT,
) -> Document:
    """Documento ingerido hasta READY con la configuración dada."""
    document = owned_document(session, storage, owner_id or add_user(session).id, content)
    ingest(session, storage, embedder, policy)
    assert document.status is DocumentStatus.READY
    return document


def reindex_all(
    session: Session, embedder: EmbeddingProvider = NEW, policy: ChunkPolicy = SMALL, **kw: Any
) -> int:
    return request_reindex(session, policy=policy, spec=embedder.spec, **kw)


def specs(session: Session, document: Document) -> set[str]:
    rows = session.execute(
        select(ChunkEmbedding.provider, ChunkEmbedding.model, ChunkEmbedding.version)
        .join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
        .where(DocumentChunk.document_id == document.id)
    )
    return {f"{provider}/{model}/v{version}" for provider, model, version in rows}


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


def status_of(document: Document) -> DocumentStatus:
    """Estado actual, sin que mypy lo dé por fijo tras una aserción anterior."""
    return document.status


def claim_one(session: Session) -> Document:
    document = claim_next(session, lease_seconds=60, max_attempts=MAX_ATTEMPTS)
    assert document is not None
    return document


def process_one(
    session: Session, storage: LocalFileStorage, document: Document, embedder: EmbeddingProvider
) -> None:
    process(session, storage, document, policy=SMALL, embedder=embedder, max_attempts=MAX_ATTEMPTS)


def process_pending_once(
    session: Session, storage: LocalFileStorage, embedder: EmbeddingProvider
) -> int:
    """Un solo intento sobre lo encolado, como un ciclo del worker; devuelve cuántos."""
    queued = session.scalars(
        select(Document).where(Document.status == DocumentStatus.UPLOADED)
    ).all()
    for _ in queued:
        process_one(session, storage, claim_one(session), embedder)
    return len(queued)


def process_pending(
    session: Session, storage: LocalFileStorage, embedder: EmbeddingProvider
) -> int:
    """Reprocesa todo lo encolado con `embedder` (como el worker) y devuelve cuántos."""
    done = 0
    while (
        document := claim_next(session, lease_seconds=60, max_attempts=MAX_ATTEMPTS)
    ) is not None:
        process_one(session, storage, document, embedder)
        done += 1
    return done


# ── Huella del índice ───────────────────────────────────────────────────────


def test_a_ready_document_records_how_it_was_indexed(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage)

    assert document.index_embedding == OLD.spec.key
    assert document.index_chunking == SMALL.key


def test_only_ready_documents_keep_a_fingerprint(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage)

    reindex_all(db_session)
    assert document.status is DocumentStatus.UPLOADED
    assert (document.index_embedding, document.index_chunking) == (None, None)

    empty = add_document(db_session, storage, b"", filename="vacio.txt", content_type="text/plain")
    ingest(db_session, storage)  # primero el reencolado, que es más antiguo
    ingest(db_session, storage)
    assert empty.status is DocumentStatus.FAILED
    assert (empty.index_embedding, empty.index_chunking) == (None, None)


def test_the_chunk_policy_key_changes_with_size_overlap_and_chunker_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert SMALL.key != BIG.key
    assert SMALL.key != ChunkPolicy(size=100, overlap=10).key
    assert SMALL.key == ChunkPolicy(size=100, overlap=20).key

    before = SMALL.key
    monkeypatch.setattr("app.ingestion.chunking.CHUNKER_VERSION", 2)
    assert SMALL.key != before


# ── Qué es obsoleto ─────────────────────────────────────────────────────────


def test_an_index_that_matches_the_current_setup_is_not_touched(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage)

    assert reindex_all(db_session, OLD, SMALL) == 0

    assert document.status is DocumentStatus.READY
    assert document.index_embedding == OLD.spec.key


@pytest.mark.parametrize(
    ("embedder", "policy"),
    [
        pytest.param(NEW, SMALL, id="otro-modelo"),
        pytest.param(NewModel("fake-model", version="2"), SMALL, id="misma-spec-otra-version"),
        pytest.param(OLD, BIG, id="otra-politica"),
        pytest.param(NEW, BIG, id="ambos"),
    ],
)
def test_a_changed_model_or_policy_makes_the_index_stale(
    db_session: Session, storage: LocalFileStorage, embedder: EmbeddingProvider, policy: ChunkPolicy
) -> None:
    document = indexed(db_session, storage)

    assert index_coverage(db_session, policy=policy, spec=embedder.spec).stale == 1
    assert reindex_all(db_session, embedder, policy) == 1
    assert document.status is DocumentStatus.UPLOADED


def test_a_new_chunker_version_makes_the_index_stale(
    db_session: Session, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = indexed(db_session, storage)
    monkeypatch.setattr("app.ingestion.chunking.CHUNKER_VERSION", 2)

    assert reindex_all(db_session, OLD, SMALL) == 1
    assert document.status is DocumentStatus.UPLOADED


def test_a_ready_document_without_a_fingerprint_is_stale(
    db_session: Session, storage: LocalFileStorage
) -> None:
    """Los READY anteriores a la huella no se sabe con qué se indexaron: se rehacen."""
    document = indexed(db_session, storage)
    document.index_embedding = None
    document.index_chunking = None
    db_session.flush()

    assert reindex_all(db_session, OLD, SMALL) == 1


def test_documents_that_are_not_ready_are_left_alone(
    db_session: Session, storage: LocalFileStorage
) -> None:
    owner = add_user(db_session).id
    queued = owned_document(db_session, storage, owner)
    failed = add_document(db_session, storage, b"", filename="a.txt", content_type="text/plain")
    ingest(db_session, storage)  # el más antiguo es `queued`: lo indexa
    ingest(db_session, storage)  # ahora `failed` queda sin texto
    waiting = owned_document(db_session, storage, owner)

    assert failed.status is DocumentStatus.FAILED
    assert waiting.status is DocumentStatus.UPLOADED
    assert reindex_all(db_session) == 1  # solo `queued`, que estaba READY
    assert failed.status is DocumentStatus.FAILED
    assert waiting.status is DocumentStatus.UPLOADED
    assert queued.status is DocumentStatus.UPLOADED


# ── Encolar es idempotente y limpio ─────────────────────────────────────────


def test_requesting_a_reindex_twice_queues_each_document_once(
    db_session: Session, storage: LocalFileStorage
) -> None:
    owner = add_user(db_session).id
    for _ in range(3):
        indexed(db_session, storage, owner_id=owner)

    assert reindex_all(db_session) == 3
    assert reindex_all(db_session) == 0


def test_queueing_resets_attempts_and_error(db_session: Session, storage: LocalFileStorage) -> None:
    document = indexed(db_session, storage)
    document.attempts = MAX_ATTEMPTS  # intentos de la pasada anterior
    document.error_summary = "algo antiguo"
    db_session.flush()

    reindex_all(db_session)

    assert document.attempts == 0
    assert document.error_summary is None
    assert process_pending(db_session, storage, NEW) == 1
    assert document.status is DocumentStatus.READY  # no falla por intentos heredados


def test_requests_can_be_limited_to_one_owner(
    db_session: Session, storage: LocalFileStorage
) -> None:
    mine = indexed(db_session, storage)
    theirs = indexed(db_session, storage)

    assert reindex_all(db_session, owner_id=mine.owner_id) == 1

    assert mine.status is DocumentStatus.UPLOADED
    assert theirs.status is DocumentStatus.READY


# ── Rehacer el índice ───────────────────────────────────────────────────────


def test_reindexing_with_a_new_model_leaves_only_new_vectors(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage)
    assert specs(db_session, document) == {"fake/fake-model/v1"}

    reindex_all(db_session)
    assert process_pending(db_session, storage, NEW) == 1

    assert document.status is DocumentStatus.READY
    assert specs(db_session, document) == {"fake/fake-model-v2/v1"}
    assert vectors(db_session, document) == len(list_chunks(db_session, document.id))
    assert document.index_embedding == NEW.spec.key
    assert reindex_all(db_session) == 0


def test_reindexing_with_a_new_policy_replaces_the_chunks(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage, policy=SMALL)
    before = len(list_chunks(db_session, document.id))

    assert reindex_all(db_session, OLD, BIG) == 1
    ingest(db_session, storage, OLD, BIG)

    after = list_chunks(db_session, document.id)
    assert len(after) < before
    assert [chunk.ordinal for chunk in after] == list(range(len(after)))
    assert vectors(db_session, document) == len(after)
    assert document.index_chunking == BIG.key


def test_repeating_the_whole_process_never_duplicates(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage)

    for _ in range(3):
        reindex_all(db_session)
        process_pending(db_session, storage, NEW)
        chunks = len(list_chunks(db_session, document.id))
        assert vectors(db_session, document) == chunks
        assert db_session.scalar(select(func.count()).select_from(ChunkEmbedding)) == chunks

    # Volver al modelo anterior también deja un único índice
    reindex_all(db_session, OLD)
    process_pending(db_session, storage, OLD)
    assert specs(db_session, document) == {"fake/fake-model/v1"}
    assert vectors(db_session, document) == len(list_chunks(db_session, document.id))


# ── Sin mezclar versiones ───────────────────────────────────────────────────


def test_a_partial_rollout_never_mixes_old_and_new_vectors_in_one_document(
    db_session: Session, storage: LocalFileStorage
) -> None:
    owner = add_user(db_session).id
    first = indexed(db_session, storage, owner_id=owner)
    second = indexed(db_session, storage, owner_id=owner, content=TEXT[::-1])
    assert reindex_all(db_session) == 2

    # El worker solo llega a migrar uno de los dos
    claimed = claim_one(db_session)
    process_one(db_session, storage, claimed, NEW)

    migrated, pending = (first, second) if claimed.id == first.id else (second, first)
    assert specs(db_session, migrated) == {"fake/fake-model-v2/v1"}
    assert specs(db_session, pending) == {"fake/fake-model/v1"}  # su índice viejo, íntegro

    query = "palabra7"
    found_new = nearest_chunks(db_session, NEW.embed_one(query), owner_id=owner, limit=50)
    found_old = nearest_chunks(db_session, OLD.embed_one(query), owner_id=owner, limit=50)
    assert {hit.document.id for hit in found_new} == {migrated.id}
    assert {hit.document.id for hit in found_old} == {pending.id}


def test_a_failure_while_reindexing_keeps_the_previous_index_complete(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = indexed(db_session, storage)
    chunks = len(list_chunks(db_session, document.id))
    reindex_all(db_session)

    assert process_pending_once(db_session, storage, NewModel(down=True)) == 1

    assert document.status is DocumentStatus.UPLOADED
    assert document.error_summary == EMBEDDING_RETRY
    assert specs(db_session, document) == {"fake/fake-model/v1"}
    assert vectors(db_session, document) == chunks
    assert (document.index_embedding, document.index_chunking) == (None, None)

    # Cuando el servicio vuelve, termina sin dejar restos del intento fallido
    assert process_pending(db_session, storage, NEW) == 1
    assert status_of(document) is DocumentStatus.READY
    assert specs(db_session, document) == {"fake/fake-model-v2/v1"}
    assert vectors(db_session, document) == len(list_chunks(db_session, document.id))


# ── Cobertura y comando ─────────────────────────────────────────────────────


def test_coverage_counts_documents_by_situation(
    db_session: Session, storage: LocalFileStorage
) -> None:
    owner = add_user(db_session).id
    indexed(db_session, storage, owner_id=owner)
    indexed(db_session, storage, owner_id=owner)
    owned_document(db_session, storage, owner)  # UPLOADED
    add_document(db_session, storage, b"", filename="a.txt", content_type="text/plain")
    ingest(db_session, storage)  # indexa el UPLOADED
    ingest(db_session, storage)  # el vacío pasa a FAILED
    owned_document(db_session, storage, owner)  # UPLOADED

    now = index_coverage(db_session, policy=SMALL, spec=OLD.spec)
    assert (now.current, now.stale, now.pending, now.failed) == (3, 0, 1, 1)
    assert now.total == 5

    after_change = index_coverage(db_session, policy=SMALL, spec=NEW.spec)
    assert (after_change.current, after_change.stale) == (0, 3)

    scoped = index_coverage(db_session, policy=SMALL, spec=OLD.spec, owner_id=owner)
    assert (scoped.current, scoped.pending, scoped.failed) == (3, 1, 0)


def test_coverage_does_not_change_anything(db_session: Session, storage: LocalFileStorage) -> None:
    document = indexed(db_session, storage)

    index_coverage(db_session, policy=SMALL, spec=NEW.spec)

    assert document.status is DocumentStatus.READY


@pytest.fixture
def cli(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[None]:
    @contextmanager
    def session() -> Iterator[Session]:
        yield db_session  # sin cerrarla: es la sesión del test

    monkeypatch.setattr(reindex, "get_sessionmaker", lambda: session)
    monkeypatch.setattr(reindex, "get_embedding_provider", lambda: NEW)
    monkeypatch.setattr(ChunkPolicy, "from_settings", classmethod(lambda c, s: SMALL))
    with caplog.at_level(logging.INFO, logger="atlas.reindex"):
        yield


def test_the_command_dry_run_reports_without_queueing(
    db_session: Session, storage: LocalFileStorage, cli: None, caplog: pytest.LogCaptureFixture
) -> None:
    document = indexed(db_session, storage)

    reindex.main(["--dry-run"])

    assert document.status is DocumentStatus.READY
    assert "1 obsoletos" in caplog.text


def test_the_command_queues_what_is_stale(
    db_session: Session, storage: LocalFileStorage, cli: None, caplog: pytest.LogCaptureFixture
) -> None:
    document = indexed(db_session, storage)

    reindex.main([])

    assert document.status is DocumentStatus.UPLOADED
    assert "1 documentos encolados" in caplog.text

    caplog.clear()
    reindex.main([])
    assert "0 documentos encolados" in caplog.text
