"""Borrar un documento: retira archivo, fragmentos y vectores, y solo lo puede hacer su dueño."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app.embeddings.models import ChunkEmbedding
from app.features.documents.deletion import DocumentBusyError, delete_owned
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.ingestion import service
from tests.test_document_flow import ALICE, BOB, ingest_all, upload
from tests.test_documents_api import URL, sign_in
from tests.test_ingestion import T0

TEXT = " ".join(f"palabra{n}" for n in range(300)).encode()


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def upload_and_index(client: TestClient, session: Session, storage: LocalFileStorage) -> Document:
    response = upload(client, "nota.txt", TEXT)
    assert response.status_code == 201
    assert ingest_all(session, storage) >= 1
    document = session.get(Document, uuid.UUID(response.json()["id"]))
    assert document is not None
    assert document.status is DocumentStatus.READY
    return document


# ── Qué se borra ────────────────────────────────────────────────────────────


def test_deleting_removes_the_file_chunks_and_vectors(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    key, document_id = document.storage_key, document.id
    assert storage.exists(key)
    assert count(db_session, DocumentChunk) > 0
    assert count(db_session, ChunkEmbedding) == count(db_session, DocumentChunk)

    response = client.delete(f"{URL}/{document_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert not storage.exists(key)
    assert db_session.get(Document, document_id) is None
    assert count(db_session, DocumentChunk) == 0
    assert count(db_session, ChunkEmbedding) == 0


def test_a_deleted_document_disappears_from_the_api(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)

    client.delete(f"{URL}/{document.id}")

    assert client.get(f"{URL}/{document.id}").status_code == 404
    assert client.get(URL).json()["total"] == 0


def test_deleting_one_document_keeps_the_rest(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    keep = upload_and_index(client, db_session, storage)
    doomed = upload_and_index(client, db_session, storage)
    kept_chunks = count(db_session, DocumentChunk) // 2

    assert client.delete(f"{URL}/{doomed.id}").status_code == 204

    assert storage.exists(keep.storage_key)
    assert client.get(f"{URL}/{keep.id}").status_code == 200
    assert count(db_session, DocumentChunk) == kept_chunks
    assert count(db_session, ChunkEmbedding) == kept_chunks


def test_a_document_that_never_finished_can_be_deleted(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    response = upload(client, "nota.txt", TEXT)
    document_id = response.json()["id"]

    assert client.delete(f"{URL}/{document_id}").status_code == 204

    assert client.get(f"{URL}/{document_id}").status_code == 404


def test_a_missing_file_does_not_block_the_deletion(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    storage.delete(document.storage_key)

    assert client.delete(f"{URL}/{document.id}").status_code == 204


def test_deleting_twice_is_a_404(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    client.delete(f"{URL}/{document.id}")

    response = client.delete(f"{URL}/{document.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


# ── Quién puede borrar ──────────────────────────────────────────────────────


def test_another_user_cannot_delete_the_document(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    chunks, vectors = count(db_session, DocumentChunk), count(db_session, ChunkEmbedding)
    client.post("/api/v1/auth/logout")
    sign_in(client, BOB)

    response = client.delete(f"{URL}/{document.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"
    missing = client.delete(f"{URL}/{uuid.uuid4()}")
    assert response.json()["error"]["message"] == missing.json()["error"]["message"]
    assert storage.exists(document.storage_key)
    assert db_session.get(Document, document.id) is not None
    assert count(db_session, DocumentChunk) == chunks
    assert count(db_session, ChunkEmbedding) == vectors


def test_the_owner_still_has_the_document_after_a_foreign_attempt(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    client.post("/api/v1/auth/logout")
    sign_in(client, BOB)
    client.delete(f"{URL}/{document.id}")
    client.post("/api/v1/auth/logout")
    sign_in(client, ALICE)

    assert client.get(f"{URL}/{document.id}").status_code == 200


def test_anonymous_users_cannot_delete(client: TestClient) -> None:
    assert client.delete(f"{URL}/{uuid.uuid4()}").status_code == 401


def test_deleting_requires_the_csrf_token(
    raw_client: TestClient, client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    del raw_client.headers["X-CSRF-Token"]

    response = raw_client.delete(f"{URL}/{document.id}")

    assert response.status_code == 403
    assert storage.exists(document.storage_key)


def test_a_malformed_id_is_rejected(client: TestClient) -> None:
    sign_in(client, ALICE)

    assert client.delete(f"{URL}/no-es-un-uuid").status_code == 422


# ── Documento en proceso ────────────────────────────────────────────────────


def claim(document: Document, *, lease_ends_in: timedelta | None) -> None:
    document.transition_to(
        DocumentStatus.PROCESSING,
        lease_expires_at=None if lease_ends_in is None else T0 + lease_ends_in,
        now=T0,
    )


def test_a_document_being_processed_cannot_be_deleted_yet(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    document.transition_to(DocumentStatus.UPLOADED)
    claim(document, lease_ends_in=timedelta(days=36500))
    db_session.flush()

    response = client.delete(f"{URL}/{document.id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_processing"
    assert storage.exists(document.storage_key)
    assert db_session.get(Document, document.id) is not None


def test_a_document_whose_worker_died_can_be_deleted(
    db_session: Session, storage: LocalFileStorage, client: TestClient
) -> None:
    owner = sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    document.transition_to(DocumentStatus.UPLOADED)
    claim(document, lease_ends_in=timedelta(seconds=60))
    db_session.flush()

    assert delete_owned(db_session, storage, owner, document.id, now=T0 + timedelta(hours=1))
    assert not storage.exists(document.storage_key)


def test_a_processing_document_without_lease_counts_as_abandoned(
    db_session: Session, storage: LocalFileStorage, client: TestClient
) -> None:
    owner = sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    document.transition_to(DocumentStatus.UPLOADED)
    claim(document, lease_ends_in=None)
    db_session.flush()

    assert delete_owned(db_session, storage, owner, document.id, now=T0)


def test_the_busy_error_is_raised_by_the_service(
    db_session: Session, storage: LocalFileStorage, client: TestClient
) -> None:
    owner = sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)
    document.transition_to(DocumentStatus.UPLOADED)
    claim(document, lease_ends_in=timedelta(seconds=60))
    db_session.flush()

    with pytest.raises(DocumentBusyError):
        delete_owned(db_session, storage, owner, document.id, now=T0)


def test_a_worker_whose_document_vanished_does_not_crash(
    db_session: Session,
    storage: LocalFileStorage,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in(client, ALICE)
    document = upload_and_index(client, db_session, storage)

    def vanished(*_args: object, **_kwargs: object) -> None:
        raise InvalidRequestError("Could not refresh instance")

    monkeypatch.setattr(db_session, "refresh", vanished)

    service._retry_or_fail(db_session, document, 3, "reintento", "fallo")
