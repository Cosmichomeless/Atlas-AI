"""Citas verificadas: documento y ubicación reales; una cita inventada no se entrega."""

import uuid

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.answers.citations import verify_citations
from app.answers.context import build_context
from app.answers.generate import Answer, generate_answer
from app.answers.grounding import strip_labels
from app.embeddings.store import nearest_chunks
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.llm.fake import FakeLLMProvider
from tests.test_embedding_store import PROVIDER, index_document
from tests.test_search_api import set_status

TEXTS = [
    "El plazo de entrega es de diez días laborables.",
    "La garantía cubre defectos de fabricación durante dos años.",
]
QUESTION = "plazo de entrega"


def indexed(session: Session) -> tuple[Document, list[DocumentChunk]]:
    document, chunks = index_document(session, TEXTS)
    set_status(document, DocumentStatus.READY)
    session.flush()
    return document, chunks


def answer_with(session: Session, document: Document, model_says: str) -> Answer:
    hits = nearest_chunks(session, PROVIDER.embed_one(QUESTION), owner_id=document.owner_id)
    context = build_context(hits, max_tokens=2000)
    return generate_answer(QUESTION, context, FakeLLMProvider("m", responder=lambda m: model_says))


def labels_of(hits_text: str, answer: Answer) -> str:
    """La etiqueta que el contexto asignó al fragmento que contiene `hits_text`."""
    return next(i.label for i in answer.context.items if hits_text in i.text)


# ── Citas válidas ───────────────────────────────────────────────────────────


def test_a_valid_citation_points_to_the_real_document_and_page(db_session: Session) -> None:
    document, chunks = indexed(db_session)
    probe = answer_with(db_session, document, "x")
    label = labels_of("plazo de entrega", probe)

    verified = verify_citations(
        db_session,
        answer_with(db_session, document, f"Diez días [{label}]."),
        owner_id=document.owner_id,
    )

    (citation,) = verified.citations
    assert verified.rejected == ()
    assert citation.label == label
    assert (citation.document_id, citation.filename) == (document.id, document.filename)
    assert citation.chunk_id == chunks[0].id
    assert (citation.page, citation.ordinal) == (1, 0)
    assert verified.text == verified.raw_text == f"Diez días [{label}]."


def test_every_distinct_label_is_resolved_once_in_order_of_appearance(
    db_session: Session,
) -> None:
    document, _ = indexed(db_session)

    verified = verify_citations(
        db_session,
        answer_with(db_session, document, "Diez días [S1]. Dos años [S2, S1]."),
        owner_id=document.owner_id,
    )

    assert [c.label for c in verified.citations] == ["S1", "S2"]
    assert verified.uncited == ()


def test_an_answer_without_citations_has_none_and_is_not_an_error(db_session: Session) -> None:
    document, _ = indexed(db_session)

    verified = verify_citations(
        db_session, answer_with(db_session, document, "No lo sé."), owner_id=document.owner_id
    )

    assert (verified.citations, verified.rejected) == ((), ())
    assert [s.kind for s in verified.statements] == ["uncited"]


# ── Citas inventadas ────────────────────────────────────────────────────────


def test_a_label_the_context_never_had_is_rejected_and_removed_from_the_text(
    db_session: Session,
) -> None:
    document, _ = indexed(db_session)

    verified = verify_citations(
        db_session,
        answer_with(db_session, document, "Diez días [S1]. Treinta años de garantía [S9]."),
        owner_id=document.owner_id,
    )

    assert [c.label for c in verified.citations] == ["S1"]
    assert [(r.label, r.reason) for r in verified.rejected] == [("S9", "unknown_label")]
    assert "[S9]" not in verified.text
    assert verified.raw_text.endswith("[S9].")


def test_a_statement_backed_only_by_an_invented_label_stops_counting_as_cited(
    db_session: Session,
) -> None:
    document, _ = indexed(db_session)

    verified = verify_citations(
        db_session,
        answer_with(db_session, document, "Diez días [S1]. Treinta años de garantía [S9]."),
        owner_id=document.owner_id,
    )

    assert [s.kind for s in verified.statements] == ["cited", "uncited"]
    assert [s.text for s in verified.uncited] == ["Treinta años de garantía."]


def test_an_invented_label_next_to_a_valid_one_is_dropped_but_the_valid_one_stays(
    db_session: Session,
) -> None:
    document, _ = indexed(db_session)

    verified = verify_citations(
        db_session,
        answer_with(db_session, document, "Diez días [S1, S7]."),
        owner_id=document.owner_id,
    )

    assert verified.text == "Diez días [S1]."
    assert [c.label for c in verified.citations] == ["S1"]
    assert [r.label for r in verified.rejected] == ["S7"]


@pytest.mark.parametrize("invented", ["[S0]", "[S01]", "[S99]", "[S3]"])
def test_labels_outside_the_context_are_never_valid(db_session: Session, invented: str) -> None:
    document, _ = indexed(db_session)

    verified = verify_citations(
        db_session,
        answer_with(db_session, document, f"Algo {invented}."),
        owner_id=document.owner_id,
    )

    assert verified.citations == ()
    assert [r.reason for r in verified.rejected] == ["unknown_label"]


# ── La fuente debe seguir existiendo y ser del usuario ──────────────────────


def test_a_chunk_deleted_after_retrieval_invalidates_its_citation(db_session: Session) -> None:
    document, chunks = indexed(db_session)
    answer = answer_with(db_session, document, "Diez días [S1].")
    cited = answer.context.item("S1")
    assert cited is not None
    db_session.execute(delete(DocumentChunk).where(DocumentChunk.id == cited.chunk_id))

    verified = verify_citations(db_session, answer, owner_id=document.owner_id)

    assert verified.citations == ()
    assert [(r.label, r.reason) for r in verified.rejected] == [("S1", "chunk_missing")]
    assert "[S1]" not in verified.text
    assert len(chunks) == 2


def test_a_chunk_that_moved_after_retrieval_invalidates_its_citation(db_session: Session) -> None:
    document, _ = indexed(db_session)
    answer = answer_with(db_session, document, "Diez días [S1].")
    cited = answer.context.item("S1")
    assert cited is not None
    db_session.execute(
        update(DocumentChunk).where(DocumentChunk.id == cited.chunk_id).values(page=40)
    )
    db_session.expire_all()

    verified = verify_citations(db_session, answer, owner_id=document.owner_id)

    assert [(r.label, r.reason) for r in verified.rejected] == [("S1", "source_changed")]


def test_a_citation_to_another_users_document_is_never_valid(db_session: Session) -> None:
    document, _ = indexed(db_session)
    answer = answer_with(db_session, document, "Diez días [S1].")

    verified = verify_citations(db_session, answer, owner_id=uuid.uuid4())

    assert verified.citations == ()
    assert [r.reason for r in verified.rejected] == ["chunk_missing"]


def test_a_document_that_is_no_longer_ready_cannot_be_cited(db_session: Session) -> None:
    document, _ = indexed(db_session)
    answer = answer_with(db_session, document, "Diez días [S1].")
    document.transition_to(DocumentStatus.UPLOADED)
    db_session.flush()

    verified = verify_citations(db_session, answer, owner_id=document.owner_id)

    assert verified.citations == ()
    assert [r.reason for r in verified.rejected] == ["chunk_missing"]


# ── strip_labels ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "invalid", "expected"),
    [
        ("Hola [S1].", [], "Hola [S1]."),
        ("Hola [S9].", ["S9"], "Hola."),
        ("Hola [S1, S9].", ["S9"], "Hola [S1]."),
        ("Hola [S9, S8] y [S1].", ["S9", "S8"], "Hola y [S1]."),
        ("Uno. [S9] Dos.", ["S9"], "Uno. Dos."),
        ("[S9] Solo.", ["S9"], "Solo."),
    ],
)
def test_strip_labels_removes_only_what_is_invalid(
    text: str, invalid: list[str], expected: str
) -> None:
    assert strip_labels(text, invalid) == expected
