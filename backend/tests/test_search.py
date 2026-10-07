"""Búsqueda top-k por similitud: orden, puntuación, metadatos y límites configurables."""

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.models import VECTOR_DIMENSIONS
from app.embeddings.provider import Embedding
from app.retrieval.dedup import DedupPolicy
from app.retrieval.question import PreparedQuestion, prepare_question
from app.retrieval.search import InvalidSearchError, SearchLimits, search_chunks
from tests.test_embedding_store import PROVIDER, index_document, other_spec

LIMITS = SearchLimits(default_k=3, max_k=5, min_score=0.0)

TEXTS = [
    "los gatos duermen todo el día en el sofá",
    "los perros ladran cuando llega el cartero",
    "la factura de la luz vence el día quince",
    "el contrato de alquiler dura doce meses",
    "el gato negro duerme en el sofá",
    "receta de paella con arroz y azafrán",
]


def ask(text: str) -> PreparedQuestion:
    return prepare_question(text, PROVIDER, max_chars=1000)


def test_results_come_ordered_by_score_with_metadata(db_session: Session) -> None:
    document, chunks = index_document(db_session, TEXTS)

    hits = search_chunks(
        db_session, ask("los gatos duermen en el sofá"), owner_id=document.owner_id, limits=LIMITS
    )

    assert [hit.chunk.ordinal for hit in hits][:2] == [0, 4]
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= score <= 1.0 for score in scores)
    top = hits[0]
    assert top.document.id == document.id
    assert top.chunk.text == TEXTS[0]
    assert (top.chunk.page, top.chunk.document_id) == (1, document.id)


def test_an_identical_text_scores_one(db_session: Session) -> None:
    document, _ = index_document(db_session, TEXTS)

    hits = search_chunks(db_session, ask(TEXTS[2]), owner_id=document.owner_id, limits=LIMITS, k=1)

    assert [hit.chunk.ordinal for hit in hits] == [2]
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)


def test_k_caps_the_number_of_results_and_defaults_to_the_configured_one(
    db_session: Session,
) -> None:
    document, _ = index_document(db_session, TEXTS)
    question = ask("el día en el sofá")

    default = search_chunks(db_session, question, owner_id=document.owner_id, limits=LIMITS)
    two = search_chunks(db_session, question, owner_id=document.owner_id, limits=LIMITS, k=2)
    every = search_chunks(
        db_session, question, owner_id=document.owner_id, limits=SearchLimits(10, 10), k=10
    )

    assert (len(default), len(two), len(every)) == (3, 2, len(TEXTS))
    assert [h.chunk.id for h in two] == [h.chunk.id for h in default][:2]


def test_the_threshold_drops_weak_matches(db_session: Session) -> None:
    document, _ = index_document(db_session, TEXTS)
    question = ask("los gatos duermen en el sofá")
    everything = search_chunks(
        db_session,
        question,
        owner_id=document.owner_id,
        limits=SearchLimits(10, 10),
        k=10,
        min_score=0.0,
    )
    cutoff = everything[1].score

    strict = search_chunks(
        db_session,
        question,
        owner_id=document.owner_id,
        limits=SearchLimits(10, 10),
        k=10,
        min_score=cutoff - 1e-6,
    )

    assert [h.chunk.id for h in strict] == [h.chunk.id for h in everything[:2]]
    assert all(hit.score >= cutoff - 1e-6 for hit in strict)


def test_nothing_above_the_threshold_gives_an_empty_list(db_session: Session) -> None:
    document, _ = index_document(db_session, TEXTS)

    hits = search_chunks(
        db_session,
        ask("astronomía cuántica"),
        owner_id=document.owner_id,
        limits=LIMITS,
        min_score=0.9,
    )

    assert hits == []


def test_the_configured_default_threshold_applies_when_none_is_given(db_session: Session) -> None:
    document, _ = index_document(db_session, TEXTS)
    limits = SearchLimits(default_k=5, max_k=5, min_score=0.99)

    hits = search_chunks(
        db_session, ask("los gatos duermen"), owner_id=document.owner_id, limits=limits
    )

    assert hits == []


def test_ties_are_resolved_deterministically(db_session: Session) -> None:
    document, chunks = index_document(db_session, ["mismo texto", "mismo texto", "mismo texto"])

    first = search_chunks(db_session, ask("mismo texto"), owner_id=document.owner_id, limits=LIMITS)
    again = search_chunks(db_session, ask("mismo texto"), owner_id=document.owner_id, limits=LIMITS)

    assert [h.chunk.ordinal for h in first] == [0, 1, 2]
    assert [h.chunk.id for h in first] == [h.chunk.id for h in again]


@pytest.mark.parametrize("k", [0, -1, 6, 1000])
def test_k_outside_the_limits_is_rejected(k: int) -> None:
    with pytest.raises(InvalidSearchError) as excinfo:
        LIMITS.resolve(k, None)

    assert excinfo.value.code == "invalid_k"
    assert "5" in excinfo.value.message


@pytest.mark.parametrize("score", [-0.1, 1.5])
def test_a_threshold_outside_zero_and_one_is_rejected(score: float) -> None:
    with pytest.raises(InvalidSearchError) as excinfo:
        LIMITS.resolve(None, score)

    assert excinfo.value.code == "invalid_min_score"


def test_limits_come_from_the_settings() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://x/y",
        search_default_k=2,
        search_max_k=7,
        search_min_score=0.25,
    )

    assert SearchLimits.from_settings(settings) == SearchLimits(
        2, 7, 0.25, 50, DedupPolicy(0.5, 1, 3)
    )


def test_a_default_k_above_the_maximum_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="SEARCH_DEFAULT_K"):
        Settings(database_url="postgresql+psycopg://x/y", search_default_k=9, search_max_k=5)


def test_the_search_ignores_vectors_of_other_models(db_session: Session) -> None:
    document, _ = index_document(db_session, TEXTS)
    foreign = PROVIDER.embed_one(TEXTS[0])
    question = PreparedQuestion("x", Embedding(foreign.vector, other_spec("9")))

    hits = search_chunks(db_session, question, owner_id=document.owner_id, limits=LIMITS)

    assert hits == []
    assert len(foreign.vector) == VECTOR_DIMENSIONS
