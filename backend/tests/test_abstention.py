"""Abstención: sin evidencia suficiente no se responde, y no es lo mismo que un error."""

from collections.abc import Sequence

import pytest
from sqlalchemy.orm import Session

from app.answers.prompt import INSUFFICIENT_MARKER
from app.answers.service import (
    Abstained,
    Answered,
    Outcome,
    answer_question,
    declares_insufficient,
)
from app.embeddings.store import nearest_chunks
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.llm.fake import FakeLLMProvider
from app.llm.provider import LLMError, Message
from tests.test_embedding_store import PROVIDER, index_document
from tests.test_search_api import set_status

TEXTS = [
    "El plazo de entrega es de diez días laborables.",
    "La garantía cubre defectos de fabricación durante dos años.",
]
QUESTION = "plazo de entrega"


def indexed(session: Session) -> Document:
    document, _ = index_document(session, TEXTS)
    set_status(document, DocumentStatus.READY)
    session.flush()
    return document


def ask(session: Session, document: Document, model_says: str | None) -> tuple[Outcome, int]:
    """Resultado de preguntar con el modelo diciendo `model_says`; y cuántas veces se le llamó."""
    provider = FakeLLMProvider(
        "m", responder=lambda messages: model_says if model_says is not None else "no debería"
    )
    hits = nearest_chunks(session, PROVIDER.embed_one(QUESTION), owner_id=document.owner_id)
    outcome = answer_question(
        session,
        QUESTION,
        hits,
        owner_id=document.owner_id,
        provider=provider,
        max_context_tokens=2000,
    )
    return outcome, len(provider.calls)


# ── Sin fragmentos relevantes ───────────────────────────────────────────────


def test_no_chunks_abstains_without_calling_the_model(db_session: Session) -> None:
    provider = FakeLLMProvider("m")

    outcome = answer_question(
        db_session,
        QUESTION,
        [],
        owner_id=indexed(db_session).owner_id,
        provider=provider,
        max_context_tokens=2000,
    )

    assert isinstance(outcome, Abstained)
    assert outcome.reason == "no_relevant_chunks"
    assert outcome.answer is None
    assert outcome.context.empty
    assert provider.calls == []


def test_chunks_that_all_exceed_the_context_budget_also_abstain(db_session: Session) -> None:
    document = indexed(db_session)
    provider = FakeLLMProvider("m")
    hits = nearest_chunks(db_session, PROVIDER.embed_one(QUESTION), owner_id=document.owner_id)

    outcome = answer_question(
        db_session,
        QUESTION,
        hits,
        owner_id=document.owner_id,
        provider=provider,
        max_context_tokens=1,
    )

    assert isinstance(outcome, Abstained)
    assert outcome.reason == "no_relevant_chunks"
    assert [o.reason for o in outcome.context.omitted] == ["over_budget"] * len(hits)
    assert provider.calls == []


# ── El modelo declara que no hay evidencia ──────────────────────────────────


@pytest.mark.parametrize(
    "says",
    [
        INSUFFICIENT_MARKER,
        f"  {INSUFFICIENT_MARKER}\n",
        f"{INSUFFICIENT_MARKER}.",
        f"`{INSUFFICIENT_MARKER}`",
        INSUFFICIENT_MARKER.lower(),
        f"Diez días [S1]. {INSUFFICIENT_MARKER}",
    ],
)
def test_the_insufficient_marker_makes_the_system_abstain(db_session: Session, says: str) -> None:
    document = indexed(db_session)

    outcome, calls = ask(db_session, document, says)

    assert isinstance(outcome, Abstained)
    assert outcome.reason == "insufficient_evidence"
    assert calls == 1
    # Lo que dijo el modelo queda para diagnóstico, pero no como respuesta.
    assert outcome.answer is not None
    assert [i.chunk_id for i in outcome.context.items] == list(outcome.answer.provenance.chunk_ids)


def test_a_word_that_merely_contains_the_marker_is_not_a_declaration() -> None:
    assert not declares_insufficient(f"X{INSUFFICIENT_MARKER}Y")
    assert not declares_insufficient("El plazo es de diez días [S1].")
    assert declares_insufficient(INSUFFICIENT_MARKER)


# ── Sin citas válidas ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "says",
    [
        "El plazo es de diez días.",  # sin ninguna cita
        "El plazo es de diez días [S9].",  # cita a un fragmento que no existe
        "(No consta en los documentos) Suele ser de una semana.",  # solo conocimiento ajeno
    ],
)
def test_an_answer_with_no_valid_citation_is_not_delivered(db_session: Session, says: str) -> None:
    document = indexed(db_session)

    outcome, _ = ask(db_session, document, says)

    assert isinstance(outcome, Abstained)
    assert outcome.reason == "no_valid_citations"
    assert outcome.answer is not None


# ── Respuesta normal ────────────────────────────────────────────────────────


def test_a_cited_answer_is_delivered_with_its_verified_citations(db_session: Session) -> None:
    document = indexed(db_session)

    outcome, calls = ask(db_session, document, "Diez días laborables [S1].")

    assert isinstance(outcome, Answered)
    assert calls == 1
    assert outcome.verified.text == "Diez días laborables [S1]."
    assert [c.label for c in outcome.verified.citations] == ["S1"]
    assert outcome.verified.citations[0].document_id == document.id


def test_a_partly_invalid_answer_is_still_delivered_without_the_invalid_citation(
    db_session: Session,
) -> None:
    document = indexed(db_session)

    outcome, _ = ask(db_session, document, "Diez días laborables [S1, S9].")

    assert isinstance(outcome, Answered)
    assert outcome.verified.text == "Diez días laborables [S1]."
    assert [r.label for r in outcome.verified.rejected] == ["S9"]


# ── Abstención ≠ error del proveedor ────────────────────────────────────────


def test_a_provider_failure_is_an_error_not_an_abstention(db_session: Session) -> None:
    document = indexed(db_session)

    def fail(messages: Sequence[Message]) -> str:
        raise LLMError("El servicio de generación respondió 503")

    hits = nearest_chunks(db_session, PROVIDER.embed_one(QUESTION), owner_id=document.owner_id)

    with pytest.raises(LLMError, match="503"):
        answer_question(
            db_session,
            QUESTION,
            hits,
            owner_id=document.owner_id,
            provider=FakeLLMProvider("m", responder=fail),
            max_context_tokens=2000,
        )


def test_abstaining_logs_the_reason_but_not_the_question(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    document = indexed(db_session)

    with caplog.at_level("INFO", logger="app.answers"):
        ask(db_session, document, INSUFFICIENT_MARKER)

    messages = [r.getMessage() for r in caplog.records]
    assert any("abstained reason=insufficient_evidence" in m for m in messages)
    assert not any(QUESTION in m for m in messages)
