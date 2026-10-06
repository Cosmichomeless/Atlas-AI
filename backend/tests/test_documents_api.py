import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from tests.factories import make_document

URL = "/api/v1/documents"
PASSWORD = "correct horse battery"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def sign_in(client: TestClient, email: str) -> uuid.UUID:
    """Registra (si hace falta) e inicia sesión como `email`; devuelve el id del usuario."""
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200
    return uuid.UUID(client.get("/api/v1/auth/me").json()["id"])


def add_documents(
    session: Session, owner: uuid.UUID, count: int, **overrides: object
) -> list[Document]:
    """`count` documentos con `created_at` escalonado (el más nuevo es el último)."""
    documents = [
        make_document(
            owner,
            filename=f"doc-{i}.pdf",
            created_at=NOW + timedelta(minutes=i),
            **overrides,
        )
        for i in range(count)
    ]
    session.add_all(documents)
    session.flush()
    return documents


def test_anonymous_users_cannot_list_or_read(client: TestClient) -> None:
    assert client.get(URL).status_code == 401
    assert client.get(f"{URL}/{uuid.uuid4()}").status_code == 401


def test_list_is_empty_for_a_new_user(client: TestClient) -> None:
    sign_in(client, "ana@example.com")

    assert client.get(URL).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_shows_metadata_and_status_newest_first(
    client: TestClient, db_session: Session
) -> None:
    ana = sign_in(client, "ana@example.com")
    add_documents(db_session, ana, 3)

    body = client.get(URL).json()

    assert [d["filename"] for d in body["items"]] == ["doc-2.pdf", "doc-1.pdf", "doc-0.pdf"]
    first = body["items"][0]
    assert first["status"] == "UPLOADED"
    assert first["content_type"] == "application/pdf"
    assert first["size_bytes"] == 1024
    assert {"id", "created_at", "updated_at"} <= first.keys()
    assert "owner_id" not in first and "lease_expires_at" not in first


def test_list_only_contains_my_documents(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    ben = sign_in(client, "ben@example.com")
    add_documents(db_session, ana, 2)
    add_documents(db_session, ben, 3)

    mine = client.get(URL).json()  # sesión actual: ben
    assert mine["total"] == 3

    sign_in(client, "ana@example.com")
    assert client.get(URL).json()["total"] == 2


def test_pagination_walks_every_document_exactly_once(
    client: TestClient, db_session: Session
) -> None:
    ana = sign_in(client, "ana@example.com")
    add_documents(db_session, ana, 5)

    pages = [client.get(URL, params={"limit": 2, "offset": offset}).json() for offset in (0, 2, 4)]

    assert [len(p["items"]) for p in pages] == [2, 2, 1]
    assert {p["total"] for p in pages} == {5}
    names = [d["filename"] for p in pages for d in p["items"]]
    assert names == [f"doc-{i}.pdf" for i in (4, 3, 2, 1, 0)]
    assert client.get(URL, params={"offset": 10}).json()["items"] == []


def test_pagination_is_stable_when_dates_tie(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    db_session.add_all([make_document(ana, created_at=NOW) for _ in range(6)])
    db_session.flush()

    ids = [
        d["id"]
        for offset in (0, 2, 4)
        for d in client.get(URL, params={"limit": 2, "offset": offset}).json()["items"]
    ]

    assert len(set(ids)) == 6


def test_filter_by_status(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    add_documents(db_session, ana, 2)
    failed = make_document(ana)
    failed.transition_to(DocumentStatus.PROCESSING)
    failed.transition_to(DocumentStatus.FAILED, error_summary="boom")
    db_session.add(failed)
    db_session.flush()

    body = client.get(URL, params={"status": "FAILED"}).json()

    assert body["total"] == 1
    assert body["items"][0]["status"] == "FAILED"
    assert client.get(URL, params={"status": "UPLOADED"}).json()["total"] == 2


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}, {"status": "DELETED"}],
)
def test_invalid_query_parameters_return_422(client: TestClient, params: dict[str, object]) -> None:
    sign_in(client, "ana@example.com")

    response = client.get(URL, params=params)  # type: ignore[arg-type]

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_detail_includes_ingestion_progress(client: TestClient, db_session: Session) -> None:
    ana = sign_in(client, "ana@example.com")
    document = make_document(ana)
    document.transition_to(DocumentStatus.PROCESSING, now=NOW)
    document.transition_to(DocumentStatus.FAILED, error_summary="PDF cifrado", now=NOW)
    db_session.add(document)
    db_session.flush()

    body = client.get(f"{URL}/{document.id}").json()

    assert body["id"] == str(document.id)
    assert body["status"] == "FAILED"
    assert body["error_summary"] == "PDF cifrado"
    assert body["attempts"] == 1
    assert body["processing_started_at"] is not None
    assert body["processed_at"] is not None
    assert "owner_id" not in body and "lease_expires_at" not in body


def test_foreign_and_missing_documents_are_indistinguishable(
    client: TestClient, db_session: Session
) -> None:
    ana = sign_in(client, "ana@example.com")
    ben = sign_in(client, "ben@example.com")
    (theirs,) = add_documents(db_session, ben, 1)
    sign_in(client, "ana@example.com")
    add_documents(db_session, ana, 1)

    foreign = client.get(f"{URL}/{theirs.id}")
    missing = client.get(f"{URL}/{uuid.uuid4()}")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == "document_not_found"
    foreign_error, missing_error = foreign.json()["error"], missing.json()["error"]
    assert foreign_error["message"] == missing_error["message"]
    assert foreign_error["details"] == missing_error["details"]
    assert "doc-0.pdf" not in foreign.text


def test_malformed_id_is_a_validation_error(client: TestClient) -> None:
    sign_in(client, "ana@example.com")

    assert client.get(f"{URL}/not-a-uuid").status_code == 422
