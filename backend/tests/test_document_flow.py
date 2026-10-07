"""Flujo documental de punta a punta: subida → worker → estado final, con dos usuarios."""

import uuid

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session

from app.features.documents.storage import LocalFileStorage
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.service import run_once
from tests.pdfs import make_pdf
from tests.test_documents_api import URL, sign_in

ALICE = "alice@example.com"
BOB = "bob@example.com"
POLICY = ChunkPolicy(size=1000, overlap=150)


def upload(client: TestClient, filename: str, content: bytes) -> Response:
    return client.post(URL, files={"file": (filename, content, "application/octet-stream")})


def ingest_all(session: Session, storage: LocalFileStorage) -> int:
    done = 0
    while run_once(session, storage, policy=POLICY, lease_seconds=60, max_attempts=3):
        done += 1
    return done


def detail(client: TestClient, document_id: str) -> dict[str, object]:
    response = client.get(f"{URL}/{document_id}")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


# ── Aislamiento entre usuarios ──────────────────────────────────────────────


def test_a_user_cannot_see_or_open_another_users_documents(client: TestClient) -> None:
    sign_in(client, ALICE)
    alice_doc = upload(client, "alice.txt", b"secreto de alice").json()["id"]
    sign_in(client, BOB)
    bob_doc = upload(client, "bob.txt", b"nota de bob").json()["id"]

    # Bob no ve el documento de Alice ni en la lista ni por ID…
    listing = client.get(URL).json()
    assert [item["id"] for item in listing["items"]] == [bob_doc]
    assert listing["total"] == 1
    foreign = client.get(f"{URL}/{alice_doc}")
    missing = client.get(f"{URL}/{uuid.uuid4()}")
    assert foreign.status_code == 404
    # …y la respuesta no distingue un documento ajeno de uno inexistente.
    assert (
        foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "document_not_found"
    )
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]

    # Y viceversa: Alice sigue viendo solo lo suyo.
    sign_in(client, ALICE)
    assert [item["id"] for item in client.get(URL).json()["items"]] == [alice_doc]
    assert client.get(f"{URL}/{bob_doc}").status_code == 404


def test_each_users_documents_are_processed_and_stay_private(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    alice_doc = upload(client, "alice.txt", b"texto de alice").json()["id"]
    sign_in(client, BOB)
    bob_doc = upload(client, "bob.pdf", make_pdf(["pagina de bob"])).json()["id"]

    assert ingest_all(db_session, storage) == 2

    assert detail(client, bob_doc)["status"] == "READY"
    assert client.get(f"{URL}/{alice_doc}").status_code == 404
    sign_in(client, ALICE)
    assert detail(client, alice_doc)["status"] == "READY"
    assert client.get(f"{URL}/{bob_doc}").status_code == 404


def test_anonymous_requests_are_rejected_at_every_step(client: TestClient) -> None:
    assert client.get(URL).status_code == 401
    assert client.get(f"{URL}/{uuid.uuid4()}").status_code == 401
    assert upload(client, "a.txt", b"hola").status_code == 401


# ── Resultados esperados por tipo de archivo ────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param("informe.pdf", make_pdf(["Hola", "Mundo"]), id="pdf-con-texto"),
        pytest.param("nota.txt", "Texto válido con acentos: áéíóú ñ.".encode(), id="texto"),
        pytest.param("guia.md", b"# Titulo\n\nUn parrafo.\n", id="markdown"),
    ],
)
def test_valid_files_end_ready(
    client: TestClient,
    db_session: Session,
    storage: LocalFileStorage,
    filename: str,
    content: bytes,
) -> None:
    sign_in(client, ALICE)
    uploaded = upload(client, filename, content)
    assert uploaded.status_code == 201
    assert uploaded.json()["status"] == "UPLOADED"

    ingest_all(db_session, storage)

    result = detail(client, uploaded.json()["id"])
    assert result["status"] == "READY"
    assert result["error_summary"] is None
    assert result["processed_at"] is not None


def test_a_pdf_without_text_ends_failed_with_a_readable_cause(
    client: TestClient, db_session: Session, storage: LocalFileStorage
) -> None:
    sign_in(client, ALICE)
    document_id = upload(client, "escaneado.pdf", make_pdf([None, None])).json()["id"]

    ingest_all(db_session, storage)

    result = detail(client, document_id)
    assert result["status"] == "FAILED"
    assert "no contiene texto extraíble" in str(result["error_summary"])


@pytest.mark.parametrize(
    ("filename", "content", "status", "code"),
    [
        pytest.param(
            "falso.pdf", b"esto no es un pdf", 415, "unsupported_media_type", id="pdf-falso"
        ),
        pytest.param("binario.txt", b"abc\x00def", 415, "unsupported_media_type", id="binario"),
        pytest.param("programa.exe", b"MZ\x90\x00", 415, "unsupported_media_type", id="extension"),
        pytest.param("vacio.txt", b"", 422, "empty_file", id="vacio"),
    ],
)
def test_invalid_files_are_rejected_and_never_reach_the_queue(
    client: TestClient,
    db_session: Session,
    storage: LocalFileStorage,
    filename: str,
    content: bytes,
    status: int,
    code: str,
) -> None:
    sign_in(client, ALICE)

    response = upload(client, filename, content)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert client.get(URL).json()["total"] == 0
    assert ingest_all(db_session, storage) == 0
