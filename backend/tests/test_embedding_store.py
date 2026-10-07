"""Vectores en pgvector: dimensión validada al guardar y similitud sin perder el origen."""

import math
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.models import VECTOR_DIMENSIONS, ChunkEmbedding
from app.embeddings.provider import Embedding, EmbeddingError, EmbeddingSpec
from app.embeddings.store import nearest_chunks, save_embeddings
from app.features.documents.models import Document, DocumentChunk
from app.ingestion.chunk_store import list_chunks, replace_chunks
from app.ingestion.chunking import Chunk
from tests.test_ingestion import add_document, add_user

PROVIDER = FakeEmbeddingProvider("fake-model", VECTOR_DIMENSIONS)


def add_chunks(session: Session, document: Document, texts: list[str]) -> list[DocumentChunk]:
    chunks = [
        Chunk(
            document.id,
            ordinal,
            body,
            page=ordinal + 1,
            section=None,
            start_line=None,
            end_line=None,
        )
        for ordinal, body in enumerate(texts)
    ]
    replace_chunks(session, document.id, chunks)
    return list(list_chunks(session, document.id))


def index_document(
    session: Session, texts: list[str], owner_id: uuid.UUID | None = None, **overrides: Any
) -> tuple[Document, list[DocumentChunk]]:
    document = add_document(session, **overrides)
    if owner_id is not None:
        document.owner_id = owner_id
    chunks = add_chunks(session, document, texts)
    save_embeddings(session, chunks, PROVIDER.embed(texts))
    return document, chunks


def stored(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ChunkEmbedding)) or 0


def other_spec(version: str) -> EmbeddingSpec:
    return EmbeddingSpec("fake", "fake-model", VECTOR_DIMENSIONS, version)


# ── Guardado ────────────────────────────────────────────────────────────────


def test_each_vector_is_stored_with_its_chunk_and_spec(db_session: Session) -> None:
    _, chunks = index_document(db_session, ["alfa beta", "gamma delta"])

    rows = db_session.scalars(select(ChunkEmbedding).order_by(ChunkEmbedding.created_at)).all()

    assert {row.chunk_id for row in rows} == {chunk.id for chunk in chunks}
    for row in rows:
        assert (row.provider, row.model, row.dimensions, row.version) == (
            "fake",
            "fake-model",
            VECTOR_DIMENSIONS,
            PROVIDER.spec.version,
        )
        assert len(row.embedding) == VECTOR_DIMENSIONS


def test_the_vector_round_trips_through_the_database(db_session: Session) -> None:
    _, chunks = index_document(db_session, ["alfa beta"])
    original = PROVIDER.embed_one("alfa beta")

    db_session.expire_all()
    row = db_session.scalars(
        select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunks[0].id)
    ).one()

    assert all(
        math.isclose(a, b, abs_tol=1e-6)
        for a, b in zip(row.embedding, original.vector, strict=True)
    )


def test_a_vector_of_another_dimension_is_rejected_before_writing(db_session: Session) -> None:
    document = add_document(db_session)
    chunks = add_chunks(db_session, document, ["alfa"])
    small = FakeEmbeddingProvider("fake-model", 8)

    with pytest.raises(EmbeddingError, match="1536"):
        save_embeddings(db_session, chunks, small.embed(["alfa"]))

    assert stored(db_session) == 0


def test_the_database_rejects_a_vector_whose_length_differs_from_the_column(
    db_session: Session,
) -> None:
    document = add_document(db_session)
    chunk = add_chunks(db_session, document, ["alfa"])[0]

    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(
            ChunkEmbedding(
                chunk_id=chunk.id,
                provider="fake",
                model="m",
                dimensions=3,
                version="1",
                embedding=[0.1, 0.2, 0.3],
            )
        )
        db_session.flush()


def test_the_database_rejects_a_declared_dimension_that_does_not_match_the_vector(
    db_session: Session,
) -> None:
    document = add_document(db_session)
    chunk = add_chunks(db_session, document, ["alfa"])[0]

    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(
            ChunkEmbedding(
                chunk_id=chunk.id,
                provider="fake",
                model="m",
                dimensions=10,
                version="1",
                embedding=[0.1] * VECTOR_DIMENSIONS,
            )
        )
        db_session.flush()


def test_the_number_of_vectors_must_match_the_number_of_chunks(db_session: Session) -> None:
    document = add_document(db_session)
    chunks = add_chunks(db_session, document, ["alfa", "beta"])

    with pytest.raises(EmbeddingError, match="1 vectores para 2"):
        save_embeddings(db_session, chunks, PROVIDER.embed(["alfa"]))

    assert stored(db_session) == 0


def test_saving_nothing_is_a_no_op(db_session: Session) -> None:
    save_embeddings(db_session, [], [])

    assert stored(db_session) == 0


def test_saving_again_with_the_same_spec_replaces_without_duplicates(db_session: Session) -> None:
    document, chunks = index_document(db_session, ["alfa", "beta"])

    save_embeddings(db_session, chunks, PROVIDER.embed(["otra cosa", "distinta"]))

    assert stored(db_session) == 2
    best = nearest_chunks(
        db_session, PROVIDER.embed_one("otra cosa"), owner_id=document.owner_id, limit=1
    )
    assert best[0].distance == pytest.approx(0, abs=1e-6)


def test_vectors_of_different_versions_coexist(db_session: Session) -> None:
    _, chunks = index_document(db_session, ["alfa"])
    newer = Embedding(PROVIDER.embed_one("alfa").vector, other_spec("2"))

    save_embeddings(db_session, chunks, [newer])

    assert stored(db_session) == 2


# ── Vínculo con fragmentos y documentos ─────────────────────────────────────


def test_deleting_the_document_deletes_its_vectors(db_session: Session) -> None:
    document, _ = index_document(db_session, ["alfa", "beta"])

    db_session.execute(delete(Document).where(Document.id == document.id))

    assert stored(db_session) == 0


def test_reprocessing_the_chunks_drops_their_old_vectors(db_session: Session) -> None:
    document, _ = index_document(db_session, ["alfa", "beta"])

    replace_chunks(db_session, document.id, [])

    assert stored(db_session) == 0


# ── Similitud ───────────────────────────────────────────────────────────────


def test_nearest_chunks_return_the_chunk_and_its_document_ordered_by_similarity(
    db_session: Session,
) -> None:
    document, chunks = index_document(
        db_session,
        [
            "el plazo de entrega es de diez días",
            "receta de tortilla con cebolla",
            "garantía de dos años",
        ],
    )

    found = nearest_chunks(
        db_session, PROVIDER.embed_one("plazo de entrega"), owner_id=document.owner_id, limit=3
    )

    assert [item.chunk.id for item in found][0] == chunks[0].id
    assert found[0].document.id == document.id
    assert found[0].chunk.page == 1
    assert [item.distance for item in found] == sorted(item.distance for item in found)
    assert len(found) == 3


def test_nearest_chunks_respect_the_limit(db_session: Session) -> None:
    document, _ = index_document(db_session, ["uno", "dos", "tres", "cuatro"])

    found = nearest_chunks(
        db_session, PROVIDER.embed_one("uno"), owner_id=document.owner_id, limit=2
    )

    assert len(found) == 2


def test_nearest_chunks_never_return_another_users_documents(db_session: Session) -> None:
    mine, _ = index_document(db_session, ["contrato de alquiler"])
    index_document(db_session, ["contrato de alquiler"], owner_id=add_user(db_session).id)

    found = nearest_chunks(
        db_session, PROVIDER.embed_one("contrato de alquiler"), owner_id=mine.owner_id
    )

    assert [item.document.id for item in found] == [mine.id]


def test_nearest_chunks_only_compare_vectors_of_the_same_spec(db_session: Session) -> None:
    document, chunks = index_document(db_session, ["alfa"])
    save_embeddings(
        db_session, chunks, [Embedding(PROVIDER.embed_one("zzz yyy").vector, other_spec("2"))]
    )

    current = nearest_chunks(db_session, PROVIDER.embed_one("alfa"), owner_id=document.owner_id)
    newer = nearest_chunks(
        db_session,
        Embedding(PROVIDER.embed_one("alfa").vector, other_spec("2")),
        owner_id=document.owner_id,
    )

    assert len(current) == len(newer) == 1
    assert current[0].distance == pytest.approx(0, abs=1e-6)
    assert newer[0].distance > 0.5


def test_nearest_chunks_reject_a_query_of_another_dimension(db_session: Session) -> None:
    query = FakeEmbeddingProvider("fake-model", 8).embed_one("alfa")

    with pytest.raises(EmbeddingError):
        nearest_chunks(db_session, query, owner_id=uuid.uuid4())


# ── Índice ──────────────────────────────────────────────────────────────────


def test_the_vector_column_has_a_cosine_hnsw_index(db_session: Session) -> None:
    definition = db_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_chunk_embeddings_embedding_cosine'"
        )
    )

    assert definition is not None
    assert "hnsw" in definition
    assert "vector_cosine_ops" in definition
