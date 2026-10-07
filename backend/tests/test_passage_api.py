import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.features.documents.models import Document, DocumentChunk
from tests.factories import make_document
from tests.test_documents_api import sign_in


def add_chunk(session: Session, document: Document, **overrides: object) -> DocumentChunk:
    fields: dict[str, object] = {
        "document_id": document.id,
        "ordinal": 0,
        "text": "El plazo de entrega es de diez días.",
        "page": 3,
        "section": "Entregas > Plazos",
    }
    chunk = DocumentChunk(**{**fields, **overrides})
    session.add(chunk)
    session.flush()
    return chunk


def passage_url(document_id: uuid.UUID, chunk_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/chunks/{chunk_id}"


def test_anonymous_users_cannot_read_a_passage(client: TestClient) -> None:
    assert client.get(passage_url(uuid.uuid4(), uuid.uuid4())).status_code == 401


def test_returns_the_original_text_and_location(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    document = make_document(ana, filename="contrato.pdf")
    db_session.add(document)
    db_session.flush()
    chunk = add_chunk(db_session, document, ordinal=2)

    response = client.get(passage_url(document.id, chunk.id))

    assert response.status_code == 200
    assert response.json() == {
        "chunk_id": str(chunk.id),
        "document_id": str(document.id),
        "filename": "contrato.pdf",
        "ordinal": 2,
        "text": "El plazo de entrega es de diez días.",
        "page": 3,
        "section": "Entregas > Plazos",
        "start_line": None,
        "end_line": None,
    }


def test_text_documents_expose_their_lines(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    document = make_document(ana, filename="notas.md", content_type="text/markdown")
    db_session.add(document)
    db_session.flush()
    chunk = add_chunk(db_session, document, page=None, start_line=4, end_line=9)

    body = client.get(passage_url(document.id, chunk.id)).json()

    assert (body["page"], body["start_line"], body["end_line"]) == (None, 4, 9)


def test_someone_elses_passage_is_indistinguishable_from_a_missing_one(
    client: TestClient, db_session: Session
) -> None:
    ana = sign_in(client, "ana@example.com")
    own = make_document(ana)
    db_session.add(own)
    db_session.flush()
    own_chunk = add_chunk(db_session, own)
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "x"})
    sign_in(client, "ben@example.com")

    foreign = client.get(passage_url(own.id, own_chunk.id))
    missing = client.get(passage_url(uuid.uuid4(), uuid.uuid4()))

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == "passage_not_found"
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert "plazo" not in foreign.text


def test_a_chunk_does_not_resolve_under_another_document(
    client: TestClient, db_session: Session
) -> None:
    ana = sign_in(client, "ana@example.com")
    first, second = make_document(ana), make_document(ana)
    db_session.add_all([first, second])
    db_session.flush()
    chunk = add_chunk(db_session, first)

    assert client.get(passage_url(second.id, chunk.id)).status_code == 404


def test_a_deleted_document_leaves_no_passage(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    document = make_document(ana)
    db_session.add(document)
    db_session.flush()
    chunk = add_chunk(db_session, document)
    assert client.get(passage_url(document.id, chunk.id)).status_code == 200

    db_session.delete(document)
    db_session.flush()

    assert client.get(passage_url(document.id, chunk.id)).status_code == 404
