"""Persistencia de fragmentos: procedencia, reprocesado sin duplicados y restricciones de BD."""

import io
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.ingestion import service
from app.ingestion.chunk_store import list_chunks, replace_chunks
from app.ingestion.chunking import Chunk, ChunkPolicy
from app.ingestion.service import run_once
from tests.pdfs import make_pdf
from tests.test_ingestion import LEASE, MAX_ATTEMPTS, POLICY, T0, add_document

SMALL = ChunkPolicy(size=100, overlap=20)


def ingest(session: Session, storage: LocalFileStorage, policy: ChunkPolicy = POLICY) -> None:
    run_once(
        session, storage, policy=policy, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS, now=T0
    )


def requeue(document: Document) -> None:
    document.transition_to(DocumentStatus.UPLOADED)


def count(session: Session, document: Document) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
        )
        or 0
    )


def text_document(session: Session, storage: LocalFileStorage, content: str) -> Document:
    return add_document(
        session,
        storage,
        content.encode(),
        filename="nota.txt",
        content_type="text/plain",
    )


def chunk(document: Document, ordinal: int = 0, **overrides: Any) -> Chunk:
    fields: dict[str, Any] = {
        "document_id": document.id,
        "ordinal": ordinal,
        "text": "texto",
        "page": None,
        "section": None,
        "start_line": None,
        "end_line": None,
    }
    return Chunk(**{**fields, **overrides})


# ── Procedencia ─────────────────────────────────────────────────────────────


def test_processing_persists_chunks_with_pdf_page_provenance(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = add_document(db_session, storage, make_pdf(["Primera página", "Segunda página"]))

    ingest(db_session, storage)

    chunks = list_chunks(db_session, document.id)
    assert document.status is DocumentStatus.READY
    assert [c.ordinal for c in chunks] == [0, 1]
    assert [c.page for c in chunks] == [1, 2]
    assert "Primera" in chunks[0].text
    assert all(c.document_id == document.id for c in chunks)


def test_processing_persists_text_line_ranges(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, "uno\ndos\n\ntres\ncuatro")

    ingest(db_session, storage)

    chunks = list_chunks(db_session, document.id)
    assert chunks
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 5
    assert all(c.page is None for c in chunks)


def test_chunks_of_a_long_document_are_ordered_and_contiguous(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, " ".join(f"palabra{i}" for i in range(200)))

    ingest(db_session, storage, SMALL)

    chunks = list_chunks(db_session, document.id)
    assert len(chunks) > 3
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(0 < len(c.text) <= SMALL.size for c in chunks)


# ── Reprocesado ─────────────────────────────────────────────────────────────


def test_reprocessing_replaces_chunks_without_duplicates(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, " ".join(f"palabra{i}" for i in range(200)))
    ingest(db_session, storage, SMALL)
    first = [c.text for c in list_chunks(db_session, document.id)]

    requeue(document)
    ingest(db_session, storage, SMALL)

    chunks = list_chunks(db_session, document.id)
    assert [c.text for c in chunks] == first
    assert [c.ordinal for c in chunks] == list(range(len(first)))
    assert count(db_session, document) == len(first)


def test_reprocessing_after_the_file_changed_leaves_only_the_new_content(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, "contenido antiguo")
    ingest(db_session, storage)
    storage.save(document.storage_key, io.BytesIO(b"contenido nuevo"))

    requeue(document)
    ingest(db_session, storage)

    texts = [c.text for c in list_chunks(db_session, document.id)]
    assert texts == ["contenido nuevo"]


def test_reprocessing_one_document_leaves_the_others_untouched(
    db_session: Session, storage: LocalFileStorage
) -> None:
    mine = text_document(db_session, storage, "mio")
    other = text_document(db_session, storage, "ajeno")
    ingest(db_session, storage)
    ingest(db_session, storage)

    requeue(mine)
    ingest(db_session, storage)

    assert [c.text for c in list_chunks(db_session, other.id)] == ["ajeno"]
    assert [c.text for c in list_chunks(db_session, mine.id)] == ["mio"]


def test_replace_chunks_with_nothing_clears_the_document(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, "algo")
    ingest(db_session, storage)

    replace_chunks(db_session, document.id, [])

    assert count(db_session, document) == 0


# ── Fallos ──────────────────────────────────────────────────────────────────


def test_a_failed_extraction_leaves_no_chunks(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = add_document(db_session, storage, make_pdf([None]))

    ingest(db_session, storage)

    assert document.status is DocumentStatus.FAILED
    assert count(db_session, document) == 0


def test_a_document_that_now_yields_no_text_fails_and_drops_stale_chunks(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, "había texto")
    ingest(db_session, storage)
    storage.save(document.storage_key, io.BytesIO(b"   \n\n  "))

    requeue(document)
    ingest(db_session, storage)

    assert document.status is DocumentStatus.FAILED
    assert count(db_session, document) == 0


def test_an_unexpected_error_keeps_the_previous_chunks(
    db_session: Session, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = text_document(db_session, storage, "estable")
    ingest(db_session, storage)
    requeue(document)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("caída a medias")

    monkeypatch.setattr(service, "replace_chunks", boom)
    ingest(db_session, storage)

    assert document.status is DocumentStatus.UPLOADED
    assert [c.text for c in list_chunks(db_session, document.id)] == ["estable"]


# ── Restricciones de la base de datos ───────────────────────────────────────


@pytest.mark.parametrize(
    "overrides",
    [
        {"text": "   "},
        {"text": ""},
        {"ordinal": -1},
        {"page": 0},
        {"start_line": 0, "end_line": 1},
        {"start_line": 3, "end_line": 2},
        {"start_line": 1, "end_line": None},
    ],
    ids=["blank", "empty", "negative-ordinal", "page-zero", "line-zero", "lines-inverted", "half"],
)
def test_the_database_rejects_invalid_chunks(
    db_session: Session, overrides: dict[str, Any]
) -> None:
    document = add_document(db_session)

    with pytest.raises(IntegrityError), db_session.begin_nested():
        replace_chunks(db_session, document.id, [chunk(document, **{"ordinal": 0, **overrides})])


def test_the_database_rejects_a_duplicate_ordinal(db_session: Session) -> None:
    document = add_document(db_session)

    with pytest.raises(IntegrityError), db_session.begin_nested():
        replace_chunks(db_session, document.id, [chunk(document, 0), chunk(document, 0)])


def test_deleting_a_document_deletes_its_chunks(db_session: Session) -> None:
    document = add_document(db_session)
    replace_chunks(db_session, document.id, [chunk(document, 0), chunk(document, 1)])

    db_session.execute(delete(Document).where(Document.id == document.id))

    assert count(db_session, document) == 0
