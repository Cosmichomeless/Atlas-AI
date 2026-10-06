from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.features.documents.models import Document
from app.features.documents.router import upload_limit_bytes
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.documents.uploads import sanitize_filename
from tests.test_documents_api import URL, sign_in

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
TEXT = "Hola, mundo. Ñandú y acentos: áéíóú.\n".encode()
MARKDOWN = b"# Titulo\n\n- uno\n- dos\n"


def upload(
    client: TestClient,
    filename: str | None,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Response:
    return client.post(URL, files={"file": (filename, content, content_type)})


def stored_files(storage: LocalFileStorage) -> list[str]:
    if not storage.root.exists():
        return []
    return sorted(str(p.relative_to(storage.root)) for p in storage.root.rglob("*") if p.is_file())


def document_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Document)) or 0


def set_limit(client: TestClient, limit: int) -> None:
    cast(FastAPI, client.app).dependency_overrides[upload_limit_bytes] = lambda: limit


@pytest.fixture
def user_client(client: TestClient) -> TestClient:
    sign_in(client, "sube@example.com")
    return client


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("informe.pdf", PDF, "application/pdf"),
        ("notas.txt", TEXT, "text/plain"),
        ("README.md", MARKDOWN, "text/markdown"),
        ("guia.markdown", MARKDOWN, "application/octet-stream"),
        ("MAYUS.PDF", PDF, "application/octet-stream"),
    ],
)
def test_valid_upload_creates_an_uploaded_document(
    user_client: TestClient,
    storage: LocalFileStorage,
    db_session: Session,
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    response = upload(user_client, filename, content, content_type)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "UPLOADED"
    assert body["filename"] == filename
    assert body["size_bytes"] == len(content)
    assert body["attempts"] == 0
    document = db_session.get(Document, body["id"])
    assert document is not None
    assert document.status is DocumentStatus.UPLOADED
    with storage.open(document.storage_key) as stored:
        assert stored.read() == content


def test_content_type_is_deduced_not_trusted(user_client: TestClient) -> None:
    body = upload(user_client, "a.txt", TEXT, "application/x-evil").json()

    assert body["content_type"] == "text/plain"
    assert "storage_key" not in body
    assert "owner_id" not in body


def test_uploaded_document_is_listed_and_readable(user_client: TestClient) -> None:
    created = upload(user_client, "informe.pdf", PDF).json()

    listing = user_client.get(URL).json()
    detail = user_client.get(f"{URL}/{created['id']}")

    assert [i["id"] for i in listing["items"]] == [created["id"]]
    assert detail.status_code == 200


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\yo\\nota.txt", "nota.txt"),
        ("  con   espacios .txt", "con espacios .txt"),
        (".oculto.txt", "oculto.txt"),
    ],
)
def test_filename_is_sanitized_and_never_used_as_a_path(
    user_client: TestClient, storage: LocalFileStorage, raw: str, expected: str
) -> None:
    response = upload(user_client, raw, TEXT)

    assert response.status_code == 201
    assert response.json()["filename"] == expected
    (path,) = stored_files(storage)
    assert expected not in path
    assert path.endswith("/original")


def test_sanitize_filename_drops_control_characters() -> None:
    assert sanitize_filename("con\x00nulo\x1b\n.txt") == "connulo.txt"
    assert sanitize_filename("tab\tulado.md") == "tabulado.md"


@pytest.mark.parametrize("filename", ["", "...", "/", "a" * 252 + ".txt"])
def test_invalid_filename_is_rejected(
    user_client: TestClient, storage: LocalFileStorage, db_session: Session, filename: str
) -> None:
    response = upload(user_client, filename, TEXT)

    assert response.status_code in (422, 415)
    assert response.json()["error"]["code"] in ("invalid_filename", "validation_error")
    assert stored_files(storage) == []
    assert document_count(db_session) == 0


def test_empty_file_is_rejected_without_leftovers(
    user_client: TestClient, storage: LocalFileStorage, db_session: Session
) -> None:
    response = upload(user_client, "vacio.txt", b"")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_file"
    assert stored_files(storage) == []
    assert document_count(db_session) == 0


def test_file_at_the_limit_is_accepted_and_above_it_rejected(
    user_client: TestClient, storage: LocalFileStorage, db_session: Session
) -> None:
    set_limit(user_client, 1000)

    assert upload(user_client, "justo.txt", b"a" * 1000).status_code == 201
    response = upload(user_client, "pasado.txt", b"a" * 1001)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert len(stored_files(storage)) == 1
    assert document_count(db_session) == 1


def test_oversized_content_length_is_rejected_early(
    user_client: TestClient, storage: LocalFileStorage
) -> None:
    set_limit(user_client, 100)

    response = user_client.post(
        URL,
        files={"file": ("a.txt", b"hola", "text/plain")},
        headers={"Content-Length": str(10 * 1024 * 1024)},
    )

    assert response.status_code == 413
    assert stored_files(storage) == []


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("virus.exe", b"MZ\x90\x00"),
        ("imagen.png", b"\x89PNG\r\n\x1a\n"),
        ("sin_extension", TEXT),
        ("doc.pdf.exe", PDF),
        ("falso.pdf", TEXT),
        ("falso.txt", PDF + b"\x00\x01\x02"),
        ("binario.txt", b"abc\x00def"),
        ("latin1.txt", "acción".encode("latin-1")),
        ("latin1.md", b"# T\xed"),
        ("truncado.txt", "ñ".encode()[:1]),
    ],
)
def test_wrong_type_or_content_is_rejected_without_leftovers(
    user_client: TestClient,
    storage: LocalFileStorage,
    db_session: Session,
    filename: str,
    content: bytes,
) -> None:
    response = upload(user_client, filename, content, "application/pdf")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert stored_files(storage) == []
    assert document_count(db_session) == 0


def test_missing_file_field_is_a_validation_error(user_client: TestClient) -> None:
    response = user_client.post(URL, data={"otro": "x"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_upload_requires_authentication(client: TestClient, storage: LocalFileStorage) -> None:
    response = upload(client, "a.txt", TEXT)

    assert response.status_code == 401
    assert stored_files(storage) == []


def test_upload_requires_csrf_token(raw_client: TestClient, storage: LocalFileStorage) -> None:
    sign_in_client = raw_client
    token = sign_in_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    sign_in_client.headers["X-CSRF-Token"] = token
    sign_in(sign_in_client, "csrf@example.com")
    del sign_in_client.headers["X-CSRF-Token"]

    response = upload(sign_in_client, "a.txt", TEXT)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert stored_files(storage) == []


def test_database_failure_removes_the_stored_file(
    user_client: TestClient,
    storage: LocalFileStorage,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("fallo de base de datos")

    monkeypatch.setattr(db_session, "commit", boom)
    safe_client = TestClient(cast(FastAPI, user_client.app), raise_server_exceptions=False)
    safe_client.cookies = user_client.cookies
    safe_client.headers.update({"X-CSRF-Token": user_client.headers["X-CSRF-Token"]})

    response = upload(safe_client, "a.txt", TEXT)

    assert response.status_code == 500
    assert stored_files(storage) == []


def test_users_upload_into_separate_keys(
    client: TestClient, storage: LocalFileStorage, db_session: Session
) -> None:
    sign_in(client, "uno@example.com")
    first = upload(client, "mismo.txt", TEXT).json()["id"]
    client.post("/api/v1/auth/logout")
    sign_in(client, "dos@example.com")
    second = upload(client, "mismo.txt", TEXT).json()["id"]

    keys = {db_session.get(Document, i).storage_key for i in (first, second)}  # type: ignore[union-attr]
    assert len(keys) == 2
    assert len(stored_files(storage)) == 2
