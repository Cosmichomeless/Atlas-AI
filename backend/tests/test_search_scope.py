"""Alcance de la búsqueda: solo documentos del propietario y, si se indica, de su selección."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.store import nearest_chunks
from app.retrieval.question import PreparedQuestion, prepare_question
from app.retrieval.search import InvalidSearchError, SearchLimits, search_chunks
from tests.test_embedding_store import PROVIDER, index_document

LIMITS = SearchLimits(default_k=10, max_k=10, min_score=0.0)
DB_URL = "postgresql+psycopg://x/y"
SHARED = "la factura de la luz vence el día quince"


def ask(text: str = SHARED) -> PreparedQuestion:
    return prepare_question(text, PROVIDER, max_chars=1000)


def test_chunks_of_other_users_never_appear_even_if_identical(db_session: Session) -> None:
    mine, _ = index_document(db_session, [SHARED, "el contrato de alquiler"])
    theirs, _ = index_document(db_session, [SHARED, "la factura del agua"])

    hits = search_chunks(db_session, ask(), owner_id=mine.owner_id, limits=LIMITS)

    assert {hit.document.id for hit in hits} == {mine.id}
    assert all(hit.document.owner_id == mine.owner_id for hit in hits)
    assert theirs.id not in {hit.document.id for hit in hits}


def test_a_selection_limits_the_search_to_those_documents(db_session: Session) -> None:
    first, _ = index_document(db_session, [SHARED, "alfa"])
    second, _ = index_document(db_session, [SHARED, "beta"], owner_id=first.owner_id)

    hits = search_chunks(
        db_session, ask(), owner_id=first.owner_id, limits=LIMITS, document_ids=[second.id]
    )

    assert {hit.document.id for hit in hits} == {second.id}
    assert len(hits) == 2


def test_the_limit_applies_after_the_selection(db_session: Session) -> None:
    """El filtro va en la consulta: un documento ajeno más parecido no desplaza al elegido."""
    chosen, _ = index_document(db_session, ["algo muy distinto", "otra cosa más"])
    index_document(db_session, [SHARED] * 3, owner_id=chosen.owner_id)

    hits = search_chunks(
        db_session, ask(), owner_id=chosen.owner_id, limits=LIMITS, k=2, document_ids=[chosen.id]
    )

    assert len(hits) == 2
    assert {hit.document.id for hit in hits} == {chosen.id}


def test_selecting_a_foreign_document_returns_nothing_without_revealing_it(
    db_session: Session,
) -> None:
    mine, _ = index_document(db_session, [SHARED])
    theirs, _ = index_document(db_session, [SHARED])
    missing = uuid.uuid4()

    foreign = search_chunks(
        db_session, ask(), owner_id=mine.owner_id, limits=LIMITS, document_ids=[theirs.id]
    )
    absent = search_chunks(
        db_session, ask(), owner_id=mine.owner_id, limits=LIMITS, document_ids=[missing]
    )

    assert foreign == absent == []


def test_a_foreign_id_in_a_mixed_selection_only_drops_that_document(db_session: Session) -> None:
    mine, _ = index_document(db_session, [SHARED])
    theirs, _ = index_document(db_session, [SHARED])

    hits = search_chunks(
        db_session,
        ask(),
        owner_id=mine.owner_id,
        limits=LIMITS,
        document_ids=[mine.id, theirs.id],
    )

    assert {hit.document.id for hit in hits} == {mine.id}


def test_an_empty_selection_searches_nothing_instead_of_everything(db_session: Session) -> None:
    mine, _ = index_document(db_session, [SHARED])

    assert (
        search_chunks(db_session, ask(), owner_id=mine.owner_id, limits=LIMITS, document_ids=[])
        == []
    )
    assert len(search_chunks(db_session, ask(), owner_id=mine.owner_id, limits=LIMITS)) == 1


def test_repeated_ids_do_not_duplicate_results(db_session: Session) -> None:
    mine, _ = index_document(db_session, [SHARED, "otro"])

    hits = search_chunks(
        db_session,
        ask(),
        owner_id=mine.owner_id,
        limits=LIMITS,
        document_ids=[mine.id, mine.id],
    )

    assert len(hits) == 2


def test_the_store_applies_both_filters_in_the_query(db_session: Session) -> None:
    mine, _ = index_document(db_session, [SHARED])
    theirs, _ = index_document(db_session, [SHARED])
    query = PROVIDER.embed_one(SHARED)

    hits = nearest_chunks(
        db_session, query, owner_id=mine.owner_id, document_ids={mine.id, theirs.id}
    )

    assert [hit.document.id for hit in hits] == [mine.id]


def test_too_many_selected_documents_are_rejected(db_session: Session) -> None:
    mine, _ = index_document(db_session, [SHARED])
    limits = SearchLimits(5, 5, 0.0, max_documents=2)

    with pytest.raises(InvalidSearchError) as error:
        search_chunks(
            db_session,
            ask(),
            owner_id=mine.owner_id,
            limits=limits,
            document_ids=[uuid.uuid4() for _ in range(3)],
        )

    assert error.value.code == "too_many_documents"
    # Los repetidos cuentan una sola vez.
    same = uuid.uuid4()
    assert (
        search_chunks(
            db_session, ask(), owner_id=mine.owner_id, limits=limits, document_ids=[same] * 5
        )
        == []
    )


def test_the_selection_limit_comes_from_settings() -> None:
    settings = Settings(database_url=DB_URL, search_max_documents=7)

    assert SearchLimits.from_settings(settings).max_documents == 7
    with pytest.raises(ValueError, match="search_max_documents"):
        Settings(database_url=DB_URL, search_max_documents=0)
