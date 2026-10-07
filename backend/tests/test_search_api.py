"""POST /api/v1/search: fragmentos con score y origen, solo de documentos READY del usuario."""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.models import VECTOR_DIMENSIONS
from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.search.router import SNIPPET_MAX_CHARS, get_search_limits, make_snippet
from app.retrieval.search import SearchLimits
from tests.test_document_flow import ALICE, BOB, ingest_all, upload
from tests.test_documents_api import sign_in
from tests.test_embedding_store import PROVIDER, index_document

URL = "/api/v1/search"
FACTURA = "la factura de la luz vence el día quince"
TEXTS = [
    "los gatos duermen todo el día en el sofá",
    FACTURA,
    "el contrato de alquiler dura doce meses",
]


@pytest.fixture(autouse=True)
def embedder(raw_client: TestClient) -> EmbeddingProvider:
    """Los vectores indexados en los tests usan `PROVIDER`; la API debe vectorizar igual."""
    raw_client.app.dependency_overrides[get_embedding_provider] = lambda: PROVIDER  # type: ignore[attr-defined]
    return PROVIDER


def ask(client: TestClient, question: str = FACTURA, **fields: Any) -> Any:
    return client.post(URL, json={"question": question, **fields})


def set_status(document: Document, status: DocumentStatus) -> None:
    """Lleva el documento a `status` por las transiciones permitidas (nace UPLOADED)."""
    if status is DocumentStatus.UPLOADED:
        return
    document.transition_to(DocumentStatus.PROCESSING)
    if status is DocumentStatus.READY:
        document.transition_to(DocumentStatus.READY)
    elif status is DocumentStatus.FAILED:
        document.transition_to(DocumentStatus.FAILED, error_summary="falló")


def ready_document(
    session: Session,
    owner: uuid.UUID,
    texts: list[str] = TEXTS,
    status: DocumentStatus = DocumentStatus.READY,
    **overrides: Any,
) -> tuple[Document, list[DocumentChunk]]:
    document, chunks = index_document(session, texts, owner_id=owner, **overrides)
    set_status(document, status)
    session.flush()
    return document, chunks


# ── Acceso ──────────────────────────────────────────────────────────────────


def test_anonymous_users_cannot_search(client: TestClient) -> None:
    assert ask(client).status_code == 401


def test_search_requires_the_csrf_token(client: TestClient) -> None:
    sign_in(client, ALICE)
    del client.headers["X-CSRF-Token"]

    assert client.post(URL, json={"question": FACTURA}).status_code == 403


# ── Forma de la respuesta ───────────────────────────────────────────────────


def test_results_carry_a_snippet_a_score_and_the_source(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    document, chunks = ready_document(db_session, alice, filename="hogar.pdf")

    body = ask(client, k=2).json()

    assert body["k"] == 2
    first = body["results"][0]
    assert first["snippet"] == FACTURA
    assert first["score"] == pytest.approx(1.0, abs=1e-6)
    assert first["source"] == {
        "document_id": str(document.id),
        "filename": "hogar.pdf",
        "chunk_id": str(chunks[1].id),
        "ordinal": 1,
        "page": 2,
        "section": None,
        "start_line": None,
        "end_line": None,
    }
    assert len(body["results"]) == 2
    scores = [result["score"] for result in body["results"]]
    assert scores == sorted(scores, reverse=True)


def test_the_response_does_not_expose_owner_or_storage_details(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)

    text = ask(client).text

    assert str(alice) not in text
    assert "storage_key" not in text
    assert "owner" not in text


def test_long_fragments_come_back_as_a_short_snippet(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    long_text = "factura " + "palabra intermedia " * 100
    ready_document(db_session, alice, [long_text])

    snippet = ask(client, "factura").json()["results"][0]["snippet"]

    assert len(snippet) <= SNIPPET_MAX_CHARS
    assert snippet.endswith("…")
    assert snippet.startswith("factura palabra")


def test_the_effective_limits_are_echoed(client: TestClient, db_session: Session) -> None:
    sign_in(client, ALICE)
    client.app.dependency_overrides[get_search_limits] = lambda: SearchLimits(  # type: ignore[attr-defined]
        default_k=4, max_k=9, min_score=0.1
    )

    body = ask(client).json()

    assert (body["k"], body["min_score"], body["results"]) == (4, 0.1, [])


# ── Solo documentos READY y autorizados ─────────────────────────────────────


@pytest.mark.parametrize(
    "status", [DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.FAILED]
)
def test_only_ready_documents_are_searched(
    client: TestClient, db_session: Session, status: DocumentStatus
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice, status=status)

    assert ask(client).json()["results"] == []


def test_a_document_being_reindexed_is_not_served_with_its_old_vectors(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    document, _ = ready_document(db_session, alice)
    assert len(ask(client).json()["results"]) == 3

    document.transition_to(DocumentStatus.UPLOADED)  # reencolado por un reindexado
    db_session.flush()

    assert ask(client).json()["results"] == []


def test_a_user_never_gets_another_users_fragments(client: TestClient, db_session: Session) -> None:
    alice = sign_in(client, ALICE)
    bob = sign_in(client, BOB)
    ready_document(db_session, alice, ["secreto de alice sobre la factura"])
    bob_doc, _ = ready_document(db_session, bob, ["nota de bob sobre la factura"])

    results = ask(client, "factura").json()["results"]

    assert [r["source"]["document_id"] for r in results] == [str(bob_doc.id)]
    assert "alice" not in str(results)
    sign_in(client, ALICE)
    assert "bob" not in str(ask(client, "factura").json())


def test_selecting_a_foreign_document_is_indistinguishable_from_an_unknown_one(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    alice_doc, _ = ready_document(db_session, alice)
    bob = sign_in(client, BOB)
    ready_document(db_session, bob)

    foreign = ask(client, document_ids=[str(alice_doc.id)])
    unknown = ask(client, document_ids=[str(uuid.uuid4())])

    assert foreign.status_code == unknown.status_code == 200
    assert foreign.json() == unknown.json()
    assert foreign.json()["results"] == []


def test_a_selection_limits_results_to_those_documents(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    first, _ = ready_document(db_session, alice)
    second, _ = ready_document(db_session, alice)

    results = ask(client, document_ids=[str(second.id)]).json()["results"]

    assert {r["source"]["document_id"] for r in results} == {str(second.id)}
    assert first.id != second.id


# ── Validación y errores ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"question": "   "}, "question_empty"),
        ({"question": "?!…"}, "question_empty"),
        ({"question": "a" * 1001}, "question_too_long"),
        ({"k": 0}, "invalid_k"),
        ({"k": 21}, "invalid_k"),
        ({"min_score": 1.5}, "invalid_min_score"),
        ({"min_score": -0.1}, "invalid_min_score"),
    ],
)
def test_invalid_parameters_are_422_with_a_stable_code(
    client: TestClient, fields: dict[str, Any], code: str
) -> None:
    sign_in(client, ALICE)

    response = ask(client, **{"question": FACTURA, **fields})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_too_many_selected_documents_is_422(client: TestClient) -> None:
    sign_in(client, ALICE)
    client.app.dependency_overrides[get_search_limits] = lambda: SearchLimits(  # type: ignore[attr-defined]
        max_documents=2
    )

    response = ask(client, document_ids=[str(uuid.uuid4()) for _ in range(3)])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "too_many_documents"


@pytest.mark.parametrize(
    "payload",
    [{}, {"question": 5}, {"question": FACTURA, "document_ids": ["no-es-uuid"]}],
)
def test_malformed_bodies_are_validation_errors(
    client: TestClient, payload: dict[str, Any]
) -> None:
    sign_in(client, ALICE)

    response = client.post(URL, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_an_index_built_with_another_model_is_a_409(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    other = FakeEmbeddingProvider("otro-modelo", VECTOR_DIMENSIONS)
    client.app.dependency_overrides[get_embedding_provider] = lambda: other  # type: ignore[attr-defined]

    response = ask(client)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "index_incompatible"
    assert "fake-model" not in error["message"]


class BrokenProvider(EmbeddingProvider):
    spec = PROVIDER.spec

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("el servicio de embeddings no responde: clave sk-secreta")


def test_a_provider_failure_is_a_503_that_leaks_nothing(client: TestClient) -> None:
    sign_in(client, ALICE)
    client.app.dependency_overrides[get_embedding_provider] = BrokenProvider  # type: ignore[attr-defined]

    response = ask(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedding_unavailable"
    assert "sk-secreta" not in response.text


def test_invalid_k_is_rejected_before_calling_the_provider(client: TestClient) -> None:
    sign_in(client, ALICE)
    client.app.dependency_overrides[get_embedding_provider] = BrokenProvider  # type: ignore[attr-defined]

    assert ask(client, k=0).json()["error"]["code"] == "invalid_k"


# ── De punta a punta ────────────────────────────────────────────────────────


def test_an_uploaded_document_becomes_searchable_once_ready(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    content = b"Primera linea sobre gatos.\n\nLa factura de la luz vence el dia quince.\n"
    document_id = upload(client, "notas.txt", content).json()["id"]

    assert ask(client, "factura de la luz").json()["results"] == []  # aún UPLOADED

    ingest_all(db_session, storage)
    results = ask(client, "factura de la luz").json()["results"]

    assert results
    assert results[0]["source"]["document_id"] == document_id
    assert results[0]["source"]["filename"] == "notas.txt"
    assert "factura" in results[0]["snippet"]


# ── Snippet ─────────────────────────────────────────────────────────────────


def test_snippets_flatten_whitespace_and_cut_on_word_boundaries() -> None:
    assert make_snippet("uno\n\n  dos\ttres") == "uno dos tres"
    cut = make_snippet("palabra " * 100, 20)
    assert cut == "palabra palabra…"
    assert make_snippet("x" * 50, 10) == "x" * 9 + "…"
    assert make_snippet("justo", 5) == "justo"
