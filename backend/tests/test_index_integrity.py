"""Fragmentos y vectores en PostgreSQL real: procedencia intacta y ningún duplicado.

Una muestra conocida (PDF y Markdown) se ingiere de punta a punta y se comprueba, fila a fila,
que conserva páginas, secciones, líneas y orden, y que cada vector pertenece a su fragmento. Luego
se reprocesa, se borra y se hace fallar el proceso a mitad por varios caminos: después de cada uno,
`assert_consistent` exige el mismo invariante global, sin filas huérfanas ni repetidas.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.embeddings.models import ChunkEmbedding
from app.embeddings.provider import Embedding
from app.embeddings.store import nearest_chunks, save_embeddings
from app.features.documents.deletion import delete_owned
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.users.models import User
from app.ingestion import service
from app.ingestion.chunk_store import list_chunks
from app.ingestion.service import claim_next
from tests.pdfs import make_pdf
from tests.test_chunk_persistence import SMALL, requeue, text_document
from tests.test_ingestion import EMBEDDER, LEASE, MAX_ATTEMPTS, T0, add_document
from tests.test_ingestion_embeddings import CountingProvider, ingest
from tests.test_reindex import status_of

LONG_PAGE = " ".join(f"termino{n:02d}" for n in range(40))
MARKDOWN = "# Guía\n\nIntro.\n\n## Instalación\n\nPaso uno.\n\n## Uso\n\nPaso dos.\n"

type Snapshot = list[tuple[uuid.UUID, int, str, int | None, str | None, int | None, int | None]]


# ── Utilidades ──────────────────────────────────────────────────────────────


def known_pdf(session: Session, storage: LocalFileStorage) -> Document:
    """Cuatro páginas: corta, larga (varios fragmentos), en blanco y corta."""
    pdf = make_pdf(["Alfa uno", LONG_PAGE, None, "Delta cuatro"])
    return add_document(session, storage, pdf)


def known_markdown(session: Session, storage: LocalFileStorage) -> Document:
    return add_document(
        session,
        storage,
        MARKDOWN.encode(),
        filename="guia.md",
        content_type="text/markdown",
    )


def total(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def snapshot(session: Session, document: Document) -> Snapshot:
    """Todo lo que identifica a los fragmentos de un documento, ids incluidos."""
    return [
        (c.id, c.ordinal, c.text, c.page, c.section, c.start_line, c.end_line)
        for c in list_chunks(session, document.id)
    ]


def content(shot: Snapshot) -> list[tuple[Any, ...]]:
    """Lo mismo sin los ids: lo que debe repetirse al reprocesar el mismo archivo."""
    return [row[1:] for row in shot]


def vectors_of(session: Session, document: Document) -> list[ChunkEmbedding]:
    return list(
        session.scalars(
            select(ChunkEmbedding)
            .join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.ordinal)
        )
    )


def assert_consistent(session: Session) -> None:
    """Invariantes de todo el índice, sea cual sea el camino que llevó hasta aquí."""
    session.expire_all()
    # Fragmentos: ordinales 0..n-1 sin huecos ni repetidos, en cada documento
    per_document = session.execute(
        select(
            DocumentChunk.document_id,
            func.count(),
            func.count(func.distinct(DocumentChunk.ordinal)),
            func.min(DocumentChunk.ordinal),
            func.max(DocumentChunk.ordinal),
        ).group_by(DocumentChunk.document_id)
    ).all()
    for document_id, rows, distinct, first, last in per_document:
        assert (rows, distinct, first, last) == (rows, rows, 0, rows - 1), document_id
    # Ni fragmentos ni vectores huérfanos
    assert not session.scalars(
        select(DocumentChunk.id)
        .outerjoin(Document, Document.id == DocumentChunk.document_id)
        .where(Document.id.is_(None))
    ).all()
    assert not session.scalars(
        select(ChunkEmbedding.id)
        .outerjoin(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
        .where(DocumentChunk.id.is_(None))
    ).all()
    # Como mucho un vector por fragmento y especificación
    repeated = session.execute(
        select(ChunkEmbedding.chunk_id)
        .group_by(
            ChunkEmbedding.chunk_id,
            ChunkEmbedding.provider,
            ChunkEmbedding.model,
            ChunkEmbedding.dimensions,
            ChunkEmbedding.version,
        )
        .having(func.count() > 1)
    ).all()
    assert not repeated
    # Un documento READY tiene todos sus fragmentos con vector de la spec con la que se indexó
    for document in session.scalars(select(Document).where(Document.status == "READY")):
        chunks = list_chunks(session, document.id)
        assert chunks, document.id
        assert len(vectors_of(session, document)) == len(chunks), document.id
        assert document.index_embedding == EMBEDDER.spec.key
        assert document.index_chunking == SMALL.key


def reprocess(session: Session, storage: LocalFileStorage, document: Document, **kw: Any) -> None:
    requeue(document)
    session.commit()
    ingest(session, storage, **kw)


# ── Una muestra conocida conserva páginas y orden ───────────────────────────


def test_a_known_pdf_keeps_its_pages_and_order(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)

    ingest(db_session, storage, CountingProvider(), SMALL)

    chunks = list_chunks(db_session, document.id)
    middle = len(chunks) - 2
    assert status_of(document) is DocumentStatus.READY
    assert middle > 2  # la página larga se parte en varios fragmentos
    # La página en blanco (3) no genera fragmentos y no desordena nada
    assert [c.page for c in chunks] == [1, *[2] * middle, 4]
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert chunks[0].text == "Alfa uno"
    assert chunks[-1].text == "Delta cuatro"
    assert all(c.section is None and c.start_line is None for c in chunks)
    # La página larga conserva todo su texto y en su orden, sin pérdidas
    body = " ".join(c.text for c in chunks[1:-1])
    positions = [body.index(f"termino{n:02d}") for n in range(40)]
    assert positions == sorted(positions)
    assert not any(c.text.strip() == "" for c in chunks)


def test_a_known_markdown_keeps_sections_lines_and_order(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_markdown(db_session, storage)

    ingest(db_session, storage, CountingProvider(), SMALL)

    rows = [
        (c.ordinal, c.section, c.start_line, c.end_line)
        for c in snapshot_rows(db_session, document)
    ]
    assert rows == [
        (0, "Guía", 1, 3),
        (1, "Guía > Instalación", 5, 7),
        (2, "Guía > Uso", 9, 11),
    ]
    assert all(c.page is None for c in list_chunks(db_session, document.id))


def snapshot_rows(session: Session, document: Document) -> Sequence[DocumentChunk]:
    return list_chunks(session, document.id)


def test_every_vector_belongs_to_the_chunk_it_was_computed_from(
    db_session: Session, storage: LocalFileStorage
) -> None:
    """Con lotes de 2 en un documento largo, el vector i sigue siendo el del fragmento i."""
    document = known_pdf(db_session, storage)
    provider = CountingProvider()

    ingest(db_session, storage, provider, SMALL)

    chunks = list_chunks(db_session, document.id)
    assert provider.calls > 2
    stored = {row.chunk_id: row for row in vectors_of(db_session, document)}
    assert set(stored) == {c.id for c in chunks}
    for chunk in chunks:
        expected = provider.embed_one(chunk.text).vector
        assert list(stored[chunk.id].embedding) == pytest.approx(list(expected), abs=1e-6)
    assert_consistent(db_session)


def test_similarity_search_returns_the_right_page_and_document(
    db_session: Session, storage: LocalFileStorage
) -> None:
    pdf = known_pdf(db_session, storage)
    guide = known_markdown(db_session, storage)
    guide.owner_id = pdf.owner_id
    db_session.flush()
    ingest(db_session, storage, CountingProvider(), SMALL)
    ingest(db_session, storage, CountingProvider(), SMALL)

    def best(text: str) -> tuple[uuid.UUID, int | None, str | None]:
        hit = nearest_chunks(db_session, EMBEDDER.embed_one(text), owner_id=pdf.owner_id, limit=1)[
            0
        ]
        return hit.chunk.document_id, hit.chunk.page, hit.chunk.section

    assert best("Alfa uno") == (pdf.id, 1, None)
    assert best("Delta cuatro") == (pdf.id, 4, None)
    assert best("Paso dos") == (guide.id, None, "Guía > Uso")
    assert best("Paso uno") == (guide.id, None, "Guía > Instalación")


# ── Reprocesar no produce duplicados ────────────────────────────────────────


def test_reprocessing_the_same_file_repeatedly_yields_identical_content(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    first = snapshot(db_session, document)
    rows = (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding))

    for _ in range(3):
        reprocess(db_session, storage, document, embedder=CountingProvider(), policy=SMALL)

        assert content(snapshot(db_session, document)) == content(first)
        assert (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding)) == rows
        assert_consistent(db_session)


def test_reprocessing_one_document_does_not_touch_another(
    db_session: Session, storage: LocalFileStorage
) -> None:
    pdf = known_pdf(db_session, storage)
    guide = known_markdown(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    ingest(db_session, storage, CountingProvider(), SMALL)
    untouched = snapshot(db_session, guide)

    reprocess(db_session, storage, pdf, embedder=CountingProvider(), policy=SMALL)

    assert snapshot(db_session, guide) == untouched  # mismos ids: ni se reescribió
    assert_consistent(db_session)


# ── Borrar no deja nada ─────────────────────────────────────────────────────


def test_deleting_leaves_no_chunks_or_vectors_and_spares_the_rest(
    db_session: Session, storage: LocalFileStorage
) -> None:
    pdf = known_pdf(db_session, storage)
    guide = known_markdown(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    ingest(db_session, storage, CountingProvider(), SMALL)
    kept = snapshot(db_session, guide)
    kept_vectors = len(vectors_of(db_session, guide))

    assert delete_owned(db_session, storage, pdf.owner_id, pdf.id)

    assert (
        db_session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == pdf.id)).all()
        == []
    )
    assert total(db_session, DocumentChunk) == len(kept)
    assert total(db_session, ChunkEmbedding) == kept_vectors
    assert snapshot(db_session, guide) == kept
    assert_consistent(db_session)


def test_deleting_a_document_awaiting_reindex_removes_its_old_index(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    requeue(document)  # espera reprocesarse y aún conserva su índice anterior
    db_session.commit()

    assert delete_owned(db_session, storage, document.owner_id, document.id)

    assert (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding)) == (0, 0)


def test_deleting_a_failed_document_after_a_failed_reprocess_leaves_nothing(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL, max_attempts=1)
    requeue(document)
    document.attempts = 1  # sin intentos de sobra: el fallo del proveedor lo da por FAILED
    db_session.commit()
    ingest(db_session, storage, CountingProvider(fail_from=2), SMALL, max_attempts=1)
    assert status_of(document) is DocumentStatus.FAILED

    assert delete_owned(db_session, storage, document.owner_id, document.id)

    assert (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding)) == (0, 0)


def test_deleting_the_user_removes_every_derived_row(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    assert total(db_session, ChunkEmbedding) > 0

    db_session.execute(delete(User).where(User.id == document.owner_id))
    db_session.expire_all()

    assert total(db_session, Document) == 0
    assert (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding)) == (0, 0)


# ── Fallar a mitad no deja restos ───────────────────────────────────────────


def test_a_failure_midway_through_a_first_ingest_leaves_nothing_then_completes(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)

    ingest(db_session, storage, CountingProvider(fail_from=2), SMALL)

    assert status_of(document) is DocumentStatus.UPLOADED
    assert (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding)) == (0, 0)
    assert_consistent(db_session)

    ingest(db_session, storage, CountingProvider(), SMALL)  # el reintento termina bien
    assert status_of(document) is DocumentStatus.READY
    assert_consistent(db_session)
    assert len(vectors_of(db_session, document)) == len(list_chunks(db_session, document.id))


def test_a_provider_failure_while_reprocessing_keeps_the_previous_index_exactly(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    before = snapshot(db_session, document)
    old_vectors = [(v.id, tuple(v.embedding)) for v in vectors_of(db_session, document)]

    reprocess(db_session, storage, document, embedder=CountingProvider(fail_from=2), policy=SMALL)

    assert snapshot(db_session, document) == before  # mismos ids: no se reescribió nada
    assert [(v.id, tuple(v.embedding)) for v in vectors_of(db_session, document)] == old_vectors
    assert_consistent_without_status(db_session, document)


def assert_consistent_without_status(session: Session, document: Document) -> None:
    """El documento volvió a la cola (UPLOADED) pero su índice anterior sigue completo."""
    assert status_of(document) is DocumentStatus.UPLOADED
    assert len(vectors_of(session, document)) == len(list_chunks(session, document.id))
    assert_consistent(session)


def test_a_database_failure_after_writing_chunks_and_vectors_rolls_everything_back(
    db_session: Session, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peor momento: fragmentos y vectores nuevos escritos y el proceso cae antes del commit."""
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    before = snapshot(db_session, document)

    def save_then_crash(
        session: Session, chunks: Sequence[DocumentChunk], embeddings: Sequence[Embedding]
    ) -> None:
        save_embeddings(session, chunks, embeddings)
        raise RuntimeError("el proceso murió justo antes del commit")

    monkeypatch.setattr("app.ingestion.service.save_embeddings", save_then_crash)

    reprocess(db_session, storage, document, embedder=CountingProvider(), policy=SMALL)

    assert snapshot(db_session, document) == before
    assert_consistent_without_status(db_session, document)


def test_a_worker_that_dies_after_claiming_leaves_nothing_and_the_retry_is_clean(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = known_pdf(db_session, storage)
    ingest(db_session, storage, CountingProvider(), SMALL)
    first = content(snapshot(db_session, document))
    requeue(document)
    db_session.commit()

    # Un worker reserva el documento y muere sin procesarlo: el arrendamiento acaba vencido
    assert claim_next(db_session, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS, now=T0)
    assert status_of(document) is DocumentStatus.PROCESSING
    later = T0 + timedelta(seconds=LEASE * 2)
    reclaimed = claim_next(db_session, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS, now=later)
    assert reclaimed is not None
    service.process(
        db_session,
        storage,
        reclaimed,
        policy=SMALL,
        embedder=CountingProvider(),
        max_attempts=MAX_ATTEMPTS,
    )

    assert status_of(document) is DocumentStatus.READY
    assert content(snapshot(db_session, document)) == first
    assert_consistent(db_session)


def test_a_file_that_stops_yielding_text_leaves_no_stale_index(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, LONG_PAGE)
    ingest(db_session, storage, CountingProvider(), SMALL)
    assert total(db_session, ChunkEmbedding) > 0
    storage.save(document.storage_key, __import__("io").BytesIO(b"   \n"))

    reprocess(db_session, storage, document, embedder=CountingProvider(), policy=SMALL)

    assert status_of(document) is DocumentStatus.FAILED
    assert (total(db_session, DocumentChunk), total(db_session, ChunkEmbedding)) == (0, 0)
    assert (document.index_embedding, document.index_chunking) == (None, None)
    assert_consistent(db_session)
