"""Integridad de citas y abstención, de punta a punta, con un modelo simulado y fuentes conocidas.

Se sube un PDF real, se ingiere y se pregunta por la API. El "modelo" es un guion: unas veces se
porta bien y otras inventa citas, texto o fuentes. Lo que se comprueba es el contrato, no el modelo:
nada de lo que llega al cliente cita algo que no existe, y sin evidencia no hay respuesta inventada.
"""

import re
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.answers.prompt import INSUFFICIENT_MARKER
from app.embeddings.registry import get_embedding_provider
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.search.router import get_search_limits
from app.llm.fake import FakeLLMProvider
from app.llm.provider import Message
from app.llm.registry import get_llm_provider
from app.retrieval.search import SearchLimits
from tests.pdfs import make_pdf
from tests.test_document_flow import ALICE, BOB, ingest_all, upload
from tests.test_documents_api import sign_in
from tests.test_embedding_store import PROVIDER

URL = "/api/v1/questions"
QUESTION = "factura de la luz"
PAGE_1 = "La factura de la luz vence el dia quince."
PAGE_2 = "Los gatos duermen en el sofa."
BOB_SECRET = "El codigo secreto de Bob es 4711."
INVENTED = "El importe de la factura es de mil euros"  # no está en ningún documento
Responder = Callable[[Sequence[Message]], str]


@pytest.fixture(autouse=True)
def embedder(raw_client: TestClient) -> None:
    raw_client.app.dependency_overrides[get_embedding_provider] = lambda: PROVIDER  # type: ignore[attr-defined]


def model(client: TestClient, says: str | Responder) -> FakeLLMProvider:
    provider = FakeLLMProvider("m", responder=says if callable(says) else lambda _: says)
    client.app.dependency_overrides[get_llm_provider] = lambda: provider  # type: ignore[attr-defined]
    return provider


def ask(client: TestClient, question: str = QUESTION, **fields: Any) -> Any:
    return client.post(URL, json={"question": question, **fields})


def ingest_report(client: TestClient, session: Session, storage: LocalFileStorage) -> str:
    """Alice sube `informe.pdf` (p. 1: factura, p. 2: gatos) y se ingiere. Devuelve su id."""
    document_id: str = upload(client, "informe.pdf", make_pdf([PAGE_1, PAGE_2])).json()["id"]
    assert ingest_all(session, storage) == 1
    return document_id


def assert_integrity(body: dict[str, Any], session: Session, owner: uuid.UUID) -> None:
    """Invariantes de cualquier respuesta, sea cual sea el guion del modelo."""
    if body["status"] == "abstained":
        assert (body["text"], body["citations"], body["uncited_statements"]) == (None, [], [])
        assert body["abstention_reason"] is not None
        return

    assert body["abstention_reason"] is None
    assert body["citations"], "una respuesta entregada necesita al menos una cita"
    labels = {c["label"] for c in body["citations"]}
    # Toda etiqueta del texto está respaldada por una cita verificada, y viceversa.
    cited_in_text = {
        label
        for group in re.findall(r"\[(S\d+(?:\s*,\s*S\d+)*)\]", body["text"])
        for label in re.findall(r"S\d+", group)
    }
    assert cited_in_text <= labels
    # Cada cita apunta a un fragmento real, listo y del usuario, en el lugar que dice.
    for citation in body["citations"]:
        row = session.execute(
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id == uuid.UUID(citation["chunk_id"]))
        ).one()
        chunk, document = row
        assert document.owner_id == owner
        assert document.status == DocumentStatus.READY
        assert str(document.id) == citation["document_id"]
        assert document.filename == citation["filename"]
        assert (chunk.page, chunk.ordinal) == (citation["page"], citation["ordinal"])
    assert {d["document_id"] for d in body["documents"]} >= {
        c["document_id"] for c in body["citations"]
    }


# ── Citas reales ────────────────────────────────────────────────────────────


def test_a_citation_resolves_to_the_exact_page_of_the_uploaded_document(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    document_id = ingest_report(client, db_session, storage)
    model(client, "Vence el día quince [S1].")

    body = ask(client).json()

    assert body["status"] == "answered"
    (citation,) = body["citations"]
    assert (citation["document_id"], citation["filename"], citation["page"]) == (
        document_id,
        "informe.pdf",
        1,
    )
    assert_integrity(body, db_session, alice)


def test_the_model_only_sees_the_known_sources_and_labels_them(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    provider = model(client, "Vence el día quince [S1].")

    ask(client)

    prompt = "\n".join(m.content for m in provider.calls[0])
    assert PAGE_1 in prompt and PAGE_2 in prompt
    assert re.findall(r"\[S\d+\] informe\.pdf", prompt) == ["[S1] informe.pdf", "[S2] informe.pdf"]


# ── Se rechazan citas inexistentes ──────────────────────────────────────────


def test_a_nonexistent_label_is_rejected_and_removed_from_the_delivered_text(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    model(client, "Vence el día quince [S1, S9]. Se paga en el banco [S7].")

    body = ask(client).json()

    assert [c["label"] for c in body["citations"]] == ["S1"]
    assert "S9" not in body["text"] and "S7" not in body["text"]
    assert body["uncited_statements"] == ["Se paga en el banco."]
    assert_integrity(body, db_session, alice)


def test_an_answer_resting_only_on_invented_citations_is_not_delivered(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    model(client, f"{INVENTED} [S5].")

    response = ask(client)

    body = response.json()
    assert (body["status"], body["abstention_reason"]) == ("abstained", "no_valid_citations")
    assert INVENTED not in response.text
    assert_integrity(body, db_session, alice)


def test_a_source_that_disappears_while_the_model_answers_cannot_be_cited(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)

    def answer_then_lose_the_sources(messages: Sequence[Message]) -> str:
        db_session.execute(delete(DocumentChunk))
        return "Vence el día quince [S1]."

    model(client, answer_then_lose_the_sources)

    body = ask(client).json()

    assert (body["status"], body["abstention_reason"]) == ("abstained", "no_valid_citations")
    assert_integrity(body, db_session, alice)


def test_a_source_whose_document_stops_being_ready_cannot_be_cited(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    document_id = ingest_report(client, db_session, storage)

    def answer_then_reindex(messages: Sequence[Message]) -> str:
        document = db_session.get(Document, uuid.UUID(document_id))
        assert document is not None
        document.transition_to(DocumentStatus.UPLOADED)
        db_session.flush()
        return "Vence el día quince [S1]."

    model(client, answer_then_reindex)

    body = ask(client).json()

    assert body["status"] == "abstained"
    assert_integrity(body, db_session, alice)


def test_a_chunk_that_moved_while_the_model_answered_is_rejected(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)

    def answer_after_a_reindex_moved_the_text(messages: Sequence[Message]) -> str:
        for chunk in db_session.scalars(select(DocumentChunk)):
            chunk.page = 99
        db_session.flush()
        return "Vence el día quince [S1]."

    model(client, answer_after_a_reindex_moved_the_text)

    body = ask(client).json()

    assert (body["status"], body["abstention_reason"]) == ("abstained", "no_valid_citations")
    assert_integrity(body, db_session, alice)


# ── Sin evidencia no hay respuesta inventada ────────────────────────────────


def test_a_question_without_any_document_abstains_and_never_reaches_the_model(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    provider = model(client, f"{INVENTED} [S1].")

    response = ask(client)

    body = response.json()
    assert (body["status"], body["abstention_reason"]) == ("abstained", "no_relevant_chunks")
    assert provider.calls == []
    assert INVENTED not in response.text
    assert_integrity(body, db_session, alice)


def test_a_question_below_the_relevance_threshold_abstains_without_calling_the_model(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    provider = model(client, f"{INVENTED} [S1].")
    client.app.dependency_overrides[get_search_limits] = lambda: SearchLimits(min_score=0.99)  # type: ignore[attr-defined]

    response = ask(client, "presupuesto anual de marketing")

    body = response.json()
    assert (body["status"], body["abstention_reason"]) == ("abstained", "no_relevant_chunks")
    assert provider.calls == []
    assert INVENTED not in response.text
    assert_integrity(body, db_session, alice)


def test_a_model_that_finds_the_sources_insufficient_yields_an_abstention(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)

    def honest_model(messages: Sequence[Message]) -> str:
        sources = re.search(r"<fuentes>(.*)</fuentes>", messages[-1].content, re.DOTALL)
        assert sources is not None
        return (
            "Vence el día quince [S1]."
            if "presupuesto" in sources.group(1)
            else INSUFFICIENT_MARKER
        )

    model(client, honest_model)

    response = ask(client, "presupuesto anual")

    body = response.json()
    assert (body["status"], body["abstention_reason"]) == ("abstained", "insufficient_evidence")
    assert INSUFFICIENT_MARKER not in response.text
    assert_integrity(body, db_session, alice)


def test_selecting_only_unrelated_scope_abstains_instead_of_answering_from_other_documents(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    provider = model(client, "Vence el día quince [S1].")

    response = ask(client, document_ids=[str(uuid.uuid4())])

    body = response.json()
    assert body["status"] == "abstained"
    assert provider.calls == []
    assert "informe.pdf" not in response.text
    assert_integrity(body, db_session, alice)


# ── Las fuentes de otro usuario no existen ──────────────────────────────────


def test_another_users_sources_never_reach_the_model_or_the_response(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, BOB)
    upload(client, "bob.pdf", make_pdf([BOB_SECRET]))
    assert ingest_all(db_session, storage) == 1
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    provider = model(client, "Vence el día quince [S1]. El código es 4711 [S3].")

    response = ask(client, "codigo secreto factura")

    body = response.json()
    prompt = "\n".join(m.content for m in provider.calls[0])
    assert "4711" not in prompt and "bob.pdf" not in prompt
    assert "bob.pdf" not in response.text
    # El modelo no pudo conocer el secreto de Bob: lo que afirma sin fuente queda marcado como tal.
    assert body["uncited_statements"] == ["El código es 4711."]
    assert [c["label"] for c in body["citations"]] == ["S1"]
    assert {c["filename"] for c in body["citations"]} == {"informe.pdf"}
    assert_integrity(body, db_session, alice)


# ── Matriz: ningún guion del modelo rompe el contrato ───────────────────────


@pytest.mark.parametrize(
    ("says", "status"),
    [
        ("Vence el día quince [S1].", "answered"),
        ("Vence el día quince [S1]. Los gatos duermen [S2].", "answered"),
        ("Vence el día quince [S1, S2, S9].", "answered"),
        ("Vence el día quince [S1] [S1] [S1].", "answered"),
        ("Vence el día quince [S0].", "abstained"),
        ("Vence el día quince [s1].", "abstained"),
        ("Vence el día quince [S 1].", "abstained"),
        ("Vence el día quince [S1000].", "abstained"),
        (f"{INVENTED}.", "abstained"),
        (f"{INVENTED} [S4].", "abstained"),
        ("(No consta en los documentos) Suele vencer a fin de mes.", "abstained"),
        (INSUFFICIENT_MARKER, "abstained"),
        (f"{INSUFFICIENT_MARKER} [S1]", "abstained"),
        (f"Vence el día quince [S1]. {INSUFFICIENT_MARKER}", "abstained"),
    ],
)
def test_no_scripted_model_can_break_the_contract(
    client: TestClient, db_session: Session, storage: LocalFileStorage, says: str, status: str
) -> None:
    alice = sign_in(client, ALICE)
    ingest_report(client, db_session, storage)
    model(client, says)

    response = ask(client)

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert_integrity(response.json(), db_session, alice)
    assert INSUFFICIENT_MARKER not in response.text
    if status == "abstained" and INVENTED in says:
        assert INVENTED not in response.text
