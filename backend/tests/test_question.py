"""Preparación de la pregunta: normalización, límites y compatibilidad con el índice."""

import unicodedata

import pytest
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.models import VECTOR_DIMENSIONS
from app.embeddings.provider import EmbeddingError, EmbeddingSpec
from app.embeddings.store import IncompatibleDimensionsError, save_embeddings
from app.ingestion.chunk_store import list_chunks
from app.retrieval.question import (
    IncompatibleIndexError,
    InvalidQuestionError,
    ensure_index_compatible,
    normalize_question,
    prepare_question,
)
from tests.test_embedding_store import PROVIDER, index_document, other_spec
from tests.test_ingestion import add_user

MAX = 100


def reason(text: str) -> str:
    with pytest.raises(InvalidQuestionError) as excinfo:
        normalize_question(text, max_chars=MAX)
    return excinfo.value.code


# ── Normalización y límites ─────────────────────────────────────────────────


def test_whitespace_is_collapsed_and_trimmed() -> None:
    assert normalize_question("  ¿Qué \n\t dice   el   contrato?  ", max_chars=MAX) == (
        "¿Qué dice el contrato?"
    )


def test_unicode_is_normalized_so_equal_questions_are_equal() -> None:
    composed = "¿Dónde está la instalación?"
    decomposed = unicodedata.normalize("NFD", composed)

    assert decomposed != composed
    assert normalize_question(decomposed, max_chars=MAX) == composed


def test_invisible_and_control_characters_are_removed() -> None:
    assert normalize_question("pa​la\u0000bra\x07 dos", max_chars=MAX) == "pala bra dos"


@pytest.mark.parametrize("text", ["", "   ", "\n\t \r\n", "​​", "?!...", " ¿¡ ?"])
def test_empty_or_contentless_questions_are_rejected(text: str) -> None:
    assert reason(text) == "question_empty"


def test_a_question_over_the_limit_is_rejected_after_normalizing() -> None:
    assert reason("a" * (MAX + 1)) == "question_too_long"
    assert normalize_question("a" * MAX, max_chars=MAX) == "a" * MAX
    # Los espacios sobrantes no cuentan: lo que se mide es la pregunta normalizada
    assert normalize_question(("a" + " " * 50) * 3, max_chars=MAX) == "a a a"


def test_messages_are_safe_to_show() -> None:
    with pytest.raises(InvalidQuestionError) as excinfo:
        normalize_question("a" * 500, max_chars=MAX)

    assert str(MAX) in excinfo.value.message


# ── Vectorización ───────────────────────────────────────────────────────────


def test_the_question_is_embedded_with_the_index_model() -> None:
    prepared = prepare_question("  Alfa   uno ", PROVIDER, max_chars=MAX)

    assert prepared.text == "Alfa uno"
    assert prepared.embedding.spec == PROVIDER.spec
    assert prepared.embedding == PROVIDER.embed_one("Alfa uno")  # igual que un fragmento idéntico


def test_an_invalid_question_never_reaches_the_provider() -> None:
    class Exploding(FakeEmbeddingProvider):
        def _embed_batch(self, texts: object) -> list[list[float]]:
            raise AssertionError("no debía llamarse al proveedor")

    provider = Exploding("fake-model", VECTOR_DIMENSIONS)

    for text in ("", "a" * (MAX + 1)):
        with pytest.raises(InvalidQuestionError):
            prepare_question(text, provider, max_chars=MAX)


def test_a_dimension_the_schema_cannot_hold_fails_before_calling_the_provider() -> None:
    class Exploding(FakeEmbeddingProvider):
        def _embed_batch(self, texts: object) -> list[list[float]]:
            raise AssertionError("no debía llamarse al proveedor")

    with pytest.raises(IncompatibleDimensionsError):
        prepare_question("hola", Exploding("fake-model", 8), max_chars=MAX)


def test_provider_failures_propagate_as_embedding_errors() -> None:
    class Failing(FakeEmbeddingProvider):
        def _embed_batch(self, texts: object) -> list[list[float]]:
            raise EmbeddingError("proveedor caído")

    with pytest.raises(EmbeddingError, match="caído"):
        prepare_question("hola", Failing("fake-model", VECTOR_DIMENSIONS), max_chars=MAX)


# ── Compatibilidad con el índice ────────────────────────────────────────────


def test_a_user_without_vectors_is_compatible(db_session: Session) -> None:
    ensure_index_compatible(db_session, PROVIDER.spec, owner_id=add_user(db_session).id)


def test_an_index_built_with_the_same_spec_is_compatible(db_session: Session) -> None:
    document, _ = index_document(db_session, ["alfa"])

    ensure_index_compatible(db_session, PROVIDER.spec, owner_id=document.owner_id)


@pytest.mark.parametrize(
    "spec",
    [
        other_spec("2"),  # otra versión del adaptador
        EmbeddingSpec("fake", "otro-modelo", VECTOR_DIMENSIONS, "1"),
        EmbeddingSpec("openai", "fake-model", VECTOR_DIMENSIONS, "1"),
    ],
)
def test_an_index_of_another_model_or_version_is_not_queried_as_compatible(
    db_session: Session, spec: EmbeddingSpec
) -> None:
    document, _ = index_document(db_session, ["alfa"])

    with pytest.raises(IncompatibleIndexError) as excinfo:
        ensure_index_compatible(db_session, spec, owner_id=document.owner_id)

    assert excinfo.value.expected == spec
    assert excinfo.value.indexed == [PROVIDER.spec.key]


def test_a_half_reindexed_index_is_compatible_if_some_vectors_match(db_session: Session) -> None:
    document, _ = index_document(db_session, ["alfa"])
    index_document(db_session, ["beta"], owner_id=document.owner_id)
    new = FakeEmbeddingProvider("fake-model", VECTOR_DIMENSIONS)
    new.spec = other_spec("2")

    save_embeddings(db_session, list_chunks(db_session, document.id), new.embed(["alfa"]))

    ensure_index_compatible(db_session, PROVIDER.spec, owner_id=document.owner_id)
    ensure_index_compatible(db_session, new.spec, owner_id=document.owner_id)


def test_other_users_vectors_do_not_make_my_index_incompatible(db_session: Session) -> None:
    index_document(db_session, ["alfa"])  # otro usuario, mismo modelo
    me = add_user(db_session)

    ensure_index_compatible(db_session, other_spec("2"), owner_id=me.id)
