"""POST /api/v1/questions: respuesta con citas y documentos consultados, o abstención."""

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.answers.prompt import INSUFFICIENT_MARKER, PROMPT_FINGERPRINT, PROMPT_VERSION
from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.features.documents.states import DocumentStatus
from app.features.search.router import get_search_limits
from app.llm.fake import FakeLLMProvider
from app.llm.provider import LLMError, Message
from app.llm.registry import get_llm_provider
from app.retrieval.search import SearchLimits
from tests.test_document_flow import ALICE, BOB
from tests.test_documents_api import sign_in
from tests.test_embedding_store import PROVIDER
from tests.test_search_api import FACTURA, ready_document

URL = "/api/v1/questions"
ANSWER = "La factura vence el día quince [S1]."


@pytest.fixture(autouse=True)
def embedder(raw_client: TestClient) -> None:
    raw_client.app.dependency_overrides[get_embedding_provider] = lambda: PROVIDER  # type: ignore[attr-defined]


class BrokenProvider(EmbeddingProvider):
    spec = PROVIDER.spec

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("el servicio de embeddings no responde")


def model(
    client: TestClient, says: str | Callable[[Sequence[Message]], str] = ANSWER
) -> FakeLLMProvider:
    provider = FakeLLMProvider("m", responder=says if callable(says) else lambda _: says)
    client.app.dependency_overrides[get_llm_provider] = lambda: provider  # type: ignore[attr-defined]
    return provider


def ask(client: TestClient, question: str = FACTURA, **fields: Any) -> Any:
    return client.post(URL, json={"question": question, **fields})


# ── Acceso ──────────────────────────────────────────────────────────────────


def test_anonymous_users_cannot_ask(client: TestClient) -> None:
    provider = model(client)

    assert ask(client).status_code == 401
    assert provider.calls == []


def test_asking_requires_the_csrf_token(client: TestClient) -> None:
    sign_in(client, ALICE)
    del client.headers["X-CSRF-Token"]

    assert client.post(URL, json={"question": FACTURA}).status_code == 403


# ── Respuesta ───────────────────────────────────────────────────────────────


def test_an_answer_carries_text_citations_documents_and_provenance(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    document, chunks = ready_document(db_session, alice, filename="hogar.pdf")
    model(client)

    response = ask(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["text"] == ANSWER
    assert body["abstention_reason"] is None
    assert body["citations"] == [
        {
            "label": "S1",
            "document_id": str(document.id),
            "filename": "hogar.pdf",
            "chunk_id": str(chunks[1].id),
            "ordinal": 1,
            "page": 2,
            "section": None,
            "start_line": None,
            "end_line": None,
        }
    ]
    assert body["documents"] == [{"document_id": str(document.id), "filename": "hogar.pdf"}]
    assert body["uncited_statements"] == []
    assert body["truncated"] is False
    assert body["provenance"] == {
        "prompt_version": PROMPT_VERSION,
        "prompt_fingerprint": PROMPT_FINGERPRINT,
        "llm": "fake/m/v1",
        "temperature": 0.0,
        "max_output_tokens": body["provenance"]["max_output_tokens"],
    }


def test_the_model_receives_the_question_and_the_users_fragments(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    provider = model(client)

    ask(client)

    (messages,) = provider.calls
    prompt = "\n".join(m.content for m in messages)
    assert FACTURA in prompt
    assert "[S1]" in prompt


def test_an_invented_citation_is_removed_from_the_delivered_text(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    model(client, "La factura vence el día quince [S1, S9].")

    body = ask(client).json()

    assert body["text"] == "La factura vence el día quince [S1]."
    assert [c["label"] for c in body["citations"]] == ["S1"]


def test_documents_are_listed_once_even_with_several_fragments(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    document, _ = ready_document(db_session, alice)
    model(client, "Vence el quince [S1]. Duerme en el sofá [S2].")

    body = ask(client).json()

    assert len(body["citations"]) == 2
    assert [d["document_id"] for d in body["documents"]] == [str(document.id)]


def test_uncited_statements_are_reported(client: TestClient, db_session: Session) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    model(client, "La factura vence el día quince [S1]. Además se paga en el banco.")

    body = ask(client).json()

    assert body["status"] == "answered"
    assert body["uncited_statements"] == ["Además se paga en el banco."]


# ── Abstención ──────────────────────────────────────────────────────────────


def test_without_documents_the_answer_is_an_abstention_and_the_model_is_not_called(
    client: TestClient,
) -> None:
    sign_in(client, ALICE)
    provider = model(client)

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == {
        "status": "abstained",
        "text": None,
        "abstention_reason": "no_relevant_chunks",
        "citations": [],
        "uncited_statements": [],
        "documents": [],
        "truncated": False,
        "provenance": None,
    }
    assert provider.calls == []


def test_the_model_declaring_insufficient_evidence_is_an_abstention(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    document, _ = ready_document(db_session, alice)
    model(client, INSUFFICIENT_MARKER)

    body = ask(client).json()

    assert (body["status"], body["text"]) == ("abstained", None)
    assert body["abstention_reason"] == "insufficient_evidence"
    assert body["documents"][0]["document_id"] == str(document.id)
    assert body["provenance"]["prompt_version"] == PROMPT_VERSION
    assert INSUFFICIENT_MARKER not in str(body["citations"])


def test_an_answer_without_a_valid_citation_is_an_abstention(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    model(client, "La factura vence el día quince [S9].")

    body = ask(client).json()

    assert (body["status"], body["abstention_reason"]) == ("abstained", "no_valid_citations")
    assert body["text"] is None
    assert "quince" not in str(body)


# ── Errores ─────────────────────────────────────────────────────────────────


def test_a_provider_failure_is_a_503_not_an_abstention(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)

    def fail(messages: Sequence[Message]) -> str:
        raise LLMError("clave sk-secreta rechazada por el servicio")

    model(client, fail)

    response = ask(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert "sk-secreta" not in response.text


def test_an_embedding_failure_is_a_503(client: TestClient, db_session: Session) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    provider = model(client)

    client.app.dependency_overrides[get_embedding_provider] = BrokenProvider  # type: ignore[attr-defined]

    response = ask(client)

    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "embedding_unavailable",
    )
    assert provider.calls == []


@pytest.mark.parametrize("question", ["", "   ", "x" * 10_001])
def test_invalid_questions_are_rejected_before_calling_the_model(
    client: TestClient, db_session: Session, question: str
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    provider = model(client)

    assert ask(client, question).status_code == 422
    assert provider.calls == []


def test_too_many_selected_documents_is_a_422(client: TestClient) -> None:
    import uuid

    sign_in(client, ALICE)
    model(client)
    client.app.dependency_overrides[get_search_limits] = lambda: SearchLimits(max_documents=2)  # type: ignore[attr-defined]

    response = ask(client, document_ids=[str(uuid.uuid4()) for _ in range(3)])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "too_many_documents"


# ── Alcance: solo READY y solo del usuario ──────────────────────────────────


@pytest.mark.parametrize(
    "status", [DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.FAILED]
)
def test_only_ready_documents_are_consulted(
    client: TestClient, db_session: Session, status: DocumentStatus
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice, status=status)
    provider = model(client)

    body = ask(client).json()

    assert body["abstention_reason"] == "no_relevant_chunks"
    assert provider.calls == []


def test_a_user_never_gets_another_users_documents(client: TestClient, db_session: Session) -> None:
    alice = sign_in(client, ALICE)
    bob = sign_in(client, BOB)
    ready_document(db_session, alice, ["secreto de alice sobre la factura"], filename="alice.pdf")
    bob_doc, _ = ready_document(db_session, bob, ["nota de bob sobre la factura"])
    provider = model(client, "Es una nota [S1].")

    body = ask(client, "factura").json()

    assert [d["document_id"] for d in body["documents"]] == [str(bob_doc.id)]
    prompt = "\n".join(m.content for m in provider.calls[0])
    assert "alice" not in prompt


def test_selecting_a_foreign_document_only_abstains(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    alice_doc, _ = ready_document(db_session, alice)
    sign_in(client, BOB)
    provider = model(client)

    body = ask(client, document_ids=[str(alice_doc.id)]).json()

    assert body["abstention_reason"] == "no_relevant_chunks"
    assert body["documents"] == []
    assert provider.calls == []


def test_the_scope_limits_which_documents_are_consulted(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    first, _ = ready_document(db_session, alice, filename="uno.pdf")
    second, _ = ready_document(db_session, alice, filename="dos.pdf")
    model(client, "Vence el quince [S1].")

    body = ask(client, document_ids=[str(second.id)]).json()

    assert [d["document_id"] for d in body["documents"]] == [str(second.id)]
    assert first.id != second.id
