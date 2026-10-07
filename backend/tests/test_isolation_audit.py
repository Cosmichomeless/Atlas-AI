"""Auditoría de aislamiento entre usuarios, de extremo a extremo (#53).

Dos usuarios, Alice y Bob, con documentos que tratan lo mismo. Se recorre cada superficie por la
que podría filtrarse contenido ajeno: archivos, fragmentos, vectores, búsqueda, contexto enviado
al modelo, respuestas y citas. Dos propiedades se comprueban siempre:

- **Sin filtración cruzada:** nada de lo que pertenece a Alice aparece en lo que recibe Bob.
- **Sin oráculo de existencia:** un ID ajeno se comporta exactamente igual que uno inexistente
  (mismo estado y mismo cuerpo), así que tampoco revela que el documento existe.

Los tests de cada módulo cubren su propia ruta; este archivo las cruza.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.models import VECTOR_DIMENSIONS, ChunkEmbedding
from app.embeddings.registry import get_embedding_provider
from app.embeddings.store import save_embeddings
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.storage import LocalFileStorage
from app.llm.provider import Message
from tests.test_document_flow import ALICE, BOB, ingest_all, upload
from tests.test_documents_api import URL as DOCUMENTS
from tests.test_documents_api import sign_in
from tests.test_embedding_store import PROVIDER
from tests.test_passage_api import passage_url
from tests.test_questions_api import ask as ask_question
from tests.test_questions_api import model
from tests.test_search_api import FACTURA, ready_document
from tests.test_search_api import ask as search

# Marcas que solo aparecen en los datos de Alice: si salen en algo de Bob, hay filtración.
ALICE_SECRET = "clave-de-alice-7731"
ALICE_FILE = "nomina-secreta-alice.pdf"
ALICE_TEXTS = [
    f"la factura de la luz vence el día quince {ALICE_SECRET}",
    "el contrato de alquiler de alice dura doce meses",
]
BOB_TEXTS = ["los gatos de bob duermen todo el día en el sofá"]
SEARCH = "/api/v1/search"
QUESTIONS = "/api/v1/questions"


@dataclass(frozen=True)
class Pair:
    """Los datos de cada usuario ya indexados; el cliente queda con la sesión de Bob."""

    alice: uuid.UUID
    bob: uuid.UUID
    alice_doc: Document
    alice_chunks: list[DocumentChunk]
    bob_doc: Document
    bob_chunks: list[DocumentChunk]


@pytest.fixture(autouse=True)
def embedder(raw_client: TestClient) -> None:
    raw_client.app.dependency_overrides[get_embedding_provider] = lambda: PROVIDER  # type: ignore[attr-defined]


@pytest.fixture
def pair(client: TestClient, db_session: Session) -> Pair:
    alice = sign_in(client, ALICE)
    alice_doc, alice_chunks = ready_document(
        db_session, alice, ALICE_TEXTS, filename=ALICE_FILE, storage_key=f"{alice}/original"
    )
    bob = sign_in(client, BOB)
    bob_doc, bob_chunks = ready_document(db_session, bob, BOB_TEXTS, filename="gatos-bob.pdf")
    return Pair(alice, bob, alice_doc, alice_chunks, bob_doc, bob_chunks)


def as_user(client: TestClient, email: str) -> None:
    sign_in(client, email)


def shape(response: Any) -> dict[str, Any]:
    """El error sin `request_id`, que es único por petición y no depende del recurso."""
    error = dict(response.json()["error"])
    error.pop("request_id")
    return error


def count(session: Session, model_: type) -> int:
    return session.scalar(select(func.count()).select_from(model_)) or 0


# ── Documentos: listar y abrir por ID ───────────────────────────────────────


class TestDocumentAccess:
    def test_the_listing_only_has_the_users_own_documents(
        self, client: TestClient, pair: Pair
    ) -> None:
        body = client.get(DOCUMENTS).json()

        assert [item["id"] for item in body["items"]] == [str(pair.bob_doc.id)]
        assert body["total"] == 1
        assert ALICE_FILE not in str(body)

    def test_a_foreign_id_is_indistinguishable_from_a_missing_one(
        self, client: TestClient, pair: Pair
    ) -> None:
        routes: list[Callable[[uuid.UUID, uuid.UUID], Any]] = [
            lambda doc, chunk: client.get(f"{DOCUMENTS}/{doc}"),
            lambda doc, chunk: client.delete(f"{DOCUMENTS}/{doc}"),
            lambda doc, chunk: client.get(passage_url(doc, chunk)),
        ]
        for route in routes:
            foreign = route(pair.alice_doc.id, pair.alice_chunks[0].id)
            missing = route(uuid.uuid4(), uuid.uuid4())

            assert foreign.status_code == missing.status_code == 404
            assert shape(foreign) == shape(missing)
            assert ALICE_SECRET not in foreign.text and ALICE_FILE not in foreign.text

    def test_a_foreign_chunk_cannot_be_read_through_the_users_own_document(
        self, client: TestClient, pair: Pair
    ) -> None:
        """Combinaciones cruzadas: el ID del documento propio no legitima el fragmento ajeno."""
        crossed = client.get(passage_url(pair.bob_doc.id, pair.alice_chunks[0].id))
        absent = client.get(passage_url(pair.bob_doc.id, uuid.uuid4()))

        assert crossed.status_code == absent.status_code == 404
        assert crossed.json()["error"]["code"] == absent.json()["error"]["code"]
        assert ALICE_SECRET not in crossed.text

    def test_an_own_chunk_cannot_be_read_through_a_foreign_document(
        self, client: TestClient, pair: Pair
    ) -> None:
        crossed = client.get(passage_url(pair.alice_doc.id, pair.bob_chunks[0].id))

        assert crossed.status_code == 404

    def test_the_owner_still_reads_everything(self, client: TestClient, pair: Pair) -> None:
        """Control: el 404 de arriba es por aislamiento, no porque las rutas no funcionen."""
        as_user(client, ALICE)

        assert client.get(f"{DOCUMENTS}/{pair.alice_doc.id}").status_code == 200
        passage = client.get(passage_url(pair.alice_doc.id, pair.alice_chunks[0].id))
        assert passage.status_code == 200
        assert ALICE_SECRET in passage.json()["text"]

    def test_malformed_ids_are_rejected_without_touching_the_data(
        self, client: TestClient, pair: Pair
    ) -> None:
        assert client.get(f"{DOCUMENTS}/{pair.alice_doc.id}'--").status_code == 422
        assert client.get(passage_url(pair.alice_doc.id, "no-uuid")).status_code == 422  # type: ignore[arg-type]


# ── Borrado y archivos ──────────────────────────────────────────────────────


class TestFilesAndDeletion:
    def test_a_foreign_delete_leaves_the_file_chunks_and_vectors_intact(
        self,
        client: TestClient,
        db_session: Session,
        storage: LocalFileStorage,
    ) -> None:
        as_user(client, ALICE)
        alice_doc = upload(client, "alice.txt", b"texto privado de alice").json()["id"]
        as_user(client, BOB)
        upload(client, "bob.txt", b"texto de bob")
        assert ingest_all(db_session, storage) == 2
        document = db_session.get(Document, uuid.UUID(alice_doc))
        assert document is not None and storage.exists(document.storage_key)
        chunks = count(db_session, DocumentChunk)
        vectors = count(db_session, ChunkEmbedding)

        response = client.delete(f"{DOCUMENTS}/{alice_doc}")

        assert response.status_code == 404
        assert storage.exists(document.storage_key)
        assert count(db_session, DocumentChunk) == chunks
        assert count(db_session, ChunkEmbedding) == vectors
        assert db_session.get(Document, uuid.UUID(alice_doc)) is not None

    def test_deleting_the_own_document_does_not_touch_the_others(
        self,
        client: TestClient,
        db_session: Session,
        storage: LocalFileStorage,
    ) -> None:
        as_user(client, ALICE)
        alice_doc = upload(client, "alice.txt", b"texto privado de alice").json()["id"]
        as_user(client, BOB)
        bob_doc = upload(client, "bob.txt", b"texto de bob").json()["id"]
        ingest_all(db_session, storage)
        alice_row = db_session.get(Document, uuid.UUID(alice_doc))
        assert alice_row is not None
        alice_chunks = db_session.scalars(
            select(DocumentChunk.id).where(DocumentChunk.document_id == alice_row.id)
        ).all()

        assert client.delete(f"{DOCUMENTS}/{bob_doc}").status_code == 204

        assert storage.exists(alice_row.storage_key)
        remaining = db_session.scalars(
            select(DocumentChunk.id).where(DocumentChunk.document_id == alice_row.id)
        ).all()
        assert sorted(remaining) == sorted(alice_chunks) and remaining
        vectors = db_session.scalars(
            select(ChunkEmbedding.chunk_id).where(ChunkEmbedding.chunk_id.in_(alice_chunks))
        ).all()
        assert sorted(vectors) == sorted(alice_chunks)

    def test_stored_files_live_under_their_owners_prefix(
        self, client: TestClient, db_session: Session
    ) -> None:
        as_user(client, ALICE)
        alice_doc = upload(client, "alice.txt", b"a").json()["id"]
        bob = sign_in(client, BOB)
        bob_doc = upload(client, "bob.txt", b"b").json()["id"]

        keys = {
            str(doc): db_session.get(Document, uuid.UUID(doc)).storage_key  # type: ignore[union-attr]
            for doc in (alice_doc, bob_doc)
        }

        assert keys[bob_doc].startswith(f"{bob}/")
        assert not keys[alice_doc].startswith(f"{bob}/")
        assert keys[alice_doc] != keys[bob_doc]


# ── Búsqueda y vectores ─────────────────────────────────────────────────────


class TestSearch:
    def test_the_other_users_matching_text_never_comes_back(
        self, client: TestClient, pair: Pair
    ) -> None:
        """La pregunta casa exactamente con un fragmento de Alice; Bob no debe verlo."""
        body = search(client, FACTURA, k=10).json()

        assert {r["source"]["document_id"] for r in body["results"]} <= {str(pair.bob_doc.id)}
        assert ALICE_SECRET not in str(body) and ALICE_FILE not in str(body)

    def test_naming_a_foreign_document_returns_what_naming_a_missing_one_does(
        self, client: TestClient, pair: Pair
    ) -> None:
        foreign = search(client, FACTURA, document_ids=[str(pair.alice_doc.id)])
        missing = search(client, FACTURA, document_ids=[str(uuid.uuid4())])

        assert foreign.status_code == missing.status_code
        assert foreign.json() == missing.json()
        assert foreign.json()["results"] == []

    def test_mixing_own_and_foreign_ids_only_searches_the_own(
        self, client: TestClient, pair: Pair
    ) -> None:
        body = search(
            client,
            FACTURA,
            k=10,
            document_ids=[str(pair.alice_doc.id), str(pair.bob_doc.id)],
        ).json()

        assert {r["source"]["document_id"] for r in body["results"]} == {str(pair.bob_doc.id)}

    def test_identical_text_in_both_accounts_is_returned_once_per_owner(
        self, client: TestClient, db_session: Session
    ) -> None:
        alice = sign_in(client, ALICE)
        alice_doc, _ = ready_document(db_session, alice)
        bob = sign_in(client, BOB)
        bob_doc, bob_chunks = ready_document(db_session, bob)

        results = search(client, FACTURA, k=10).json()["results"]

        assert {r["source"]["document_id"] for r in results} == {str(bob_doc.id)}
        assert {r["source"]["chunk_id"] for r in results} == {str(c.id) for c in bob_chunks}
        assert str(alice_doc.id) not in str(results)

    def test_the_owner_does_find_it(self, client: TestClient, pair: Pair) -> None:
        """Control: sin el aislamiento, el mismo texto sí aparece para Alice."""
        as_user(client, ALICE)

        top = search(client, FACTURA).json()["results"][0]

        assert top["source"]["document_id"] == str(pair.alice_doc.id)

    def test_an_index_built_with_another_model_only_blocks_its_owner(
        self, client: TestClient, db_session: Session
    ) -> None:
        """El 409 depende solo de los vectores propios: el índice de Alice no afecta a Bob."""
        alice = sign_in(client, ALICE)
        document, chunks = ready_document(db_session, alice, ALICE_TEXTS)
        other = FakeEmbeddingProvider("otro-modelo", VECTOR_DIMENSIONS)
        db_session.query(ChunkEmbedding).filter(
            ChunkEmbedding.chunk_id.in_([c.id for c in chunks])
        ).delete()
        save_embeddings(db_session, chunks, other.embed([c.text for c in chunks]))
        bob = sign_in(client, BOB)
        ready_document(db_session, bob, BOB_TEXTS)

        assert search(client, "gatos").status_code == 200
        as_user(client, ALICE)
        blocked = search(client, "gatos")
        assert blocked.status_code == 409
        assert str(document.id) not in blocked.text


# ── Respuestas y citas ──────────────────────────────────────────────────────


class TestAnswers:
    def test_the_model_never_receives_the_other_users_text(
        self, client: TestClient, pair: Pair
    ) -> None:
        provider = model(client)

        ask_question(client, FACTURA)

        for messages in provider.calls:
            prompt = "\n".join(m.content for m in messages)
            assert ALICE_SECRET not in prompt
            assert "alice" not in prompt.lower()

    def test_without_own_evidence_the_model_is_not_called_at_all(
        self, client: TestClient, db_session: Session
    ) -> None:
        sign_in(client, ALICE)
        ready_document(db_session, uuid.UUID(client.get("/api/v1/auth/me").json()["id"]))
        sign_in(client, BOB)  # Bob no tiene documentos
        provider = model(client)

        body = ask_question(client, FACTURA).json()

        assert body["status"] == "abstained"
        assert provider.calls == []
        assert body["citations"] == [] and body["documents"] == []

    def test_citations_and_documents_only_point_at_own_sources(
        self, client: TestClient, pair: Pair
    ) -> None:
        model(client, "los gatos duermen en el sofá [S1].")

        body = ask_question(client, "los gatos duermen todo el día", k=10).json()

        assert body["status"] == "answered"
        assert {c["document_id"] for c in body["citations"]} == {str(pair.bob_doc.id)}
        assert {d["document_id"] for d in body["documents"]} == {str(pair.bob_doc.id)}
        assert ALICE_FILE not in str(body)

    def test_a_label_the_model_invents_cannot_reach_another_users_chunk(
        self, client: TestClient, pair: Pair
    ) -> None:
        """Aunque el modelo cite etiquetas de más, solo valen las del contexto de Bob."""
        model(client, "los gatos duermen [S1]. La clave es otra [S2] [S3] [S99].")

        body = ask_question(client, "los gatos duermen todo el día").json()

        owned = {str(c.id) for c in pair.bob_chunks}
        assert {c["chunk_id"] for c in body["citations"]} <= owned
        assert ALICE_SECRET not in str(body)

    def test_naming_a_foreign_document_gives_no_sources(
        self, client: TestClient, pair: Pair
    ) -> None:
        provider = model(client)

        foreign = ask_question(client, FACTURA, document_ids=[str(pair.alice_doc.id)])
        missing = ask_question(client, FACTURA, document_ids=[str(uuid.uuid4())])

        assert foreign.status_code == missing.status_code == 200
        assert foreign.json() == missing.json()
        assert foreign.json()["citations"] == []
        assert provider.calls == []

    def test_a_responder_that_echoes_its_prompt_cannot_leak_it(
        self, client: TestClient, pair: Pair
    ) -> None:
        """Lo peor que haría un modelo es repetir todo lo que recibió: eso es solo lo de Bob."""

        def echo(messages: Sequence[Message]) -> str:
            return "\n".join(m.content for m in messages) + " [S1]"

        model(client, echo)

        response = ask_question(client, FACTURA)

        assert ALICE_SECRET not in response.text and ALICE_FILE not in response.text


# ── Reindexado ──────────────────────────────────────────────────────────────


class TestReindex:
    def test_reindex_scoped_to_a_user_does_not_requeue_anyone_else(
        self, db_session: Session, pair: Pair
    ) -> None:
        from app.ingestion.chunking import ChunkPolicy
        from app.ingestion.reindex import index_coverage, request_reindex

        policy = ChunkPolicy(size=1000, overlap=150)
        other_spec = FakeEmbeddingProvider("otro-modelo", VECTOR_DIMENSIONS).spec

        before_alice = index_coverage(
            db_session, policy=policy, spec=other_spec, owner_id=pair.alice
        )
        queued = request_reindex(db_session, policy=policy, spec=other_spec, owner_id=pair.bob)

        assert before_alice.stale == 1
        assert queued == 1
        assert index_coverage(db_session, policy=policy, spec=other_spec, owner_id=pair.alice) == (
            before_alice
        )
        db_session.refresh(pair.alice_doc)
        assert pair.alice_doc.status.value == "READY"
