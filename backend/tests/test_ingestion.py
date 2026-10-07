import io
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.users.models import User
from app.ingestion import service
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.service import claim_next, process, run_once
from app.ingestion.worker import run
from tests.factories import make_document
from tests.pdfs import make_pdf
from tests.test_documents_api import URL, sign_in

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LEASE = 60
MAX_ATTEMPTS = 3
POLICY = ChunkPolicy(size=1000, overlap=150)
EMBEDDER = FakeEmbeddingProvider("fake-model", 1536)


def add_user(session: Session) -> User:
    user = User(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    session.add(user)
    session.flush()
    return user


def add_document(
    session: Session,
    storage: LocalFileStorage | None = None,
    content: bytes | None = None,
    **overrides: Any,
) -> Document:
    document = make_document(add_user(session).id, **overrides)
    session.add(document)
    session.flush()
    if storage is not None and content is not None:
        storage.save(document.storage_key, io.BytesIO(content))
    return document


def claim(session: Session, now: datetime = T0) -> Document | None:
    return claim_next(session, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS, now=now)


# ── La subida no espera al procesamiento ────────────────────────────────────


def test_upload_returns_before_any_extraction_happens(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("la API no debe extraer texto")

    monkeypatch.setattr("app.features.documents.extraction.extract_blocks", explode)
    sign_in(client, "worker@example.com")

    response = client.post(URL, files={"file": ("nota.txt", b"hola mundo", "text/plain")})

    assert response.status_code == 201
    assert response.json()["status"] == "UPLOADED"


# ── Reserva ─────────────────────────────────────────────────────────────────


def test_claim_returns_none_when_there_is_no_work(db_session: Session) -> None:
    assert claim(db_session) is None


def test_claim_takes_the_oldest_uploaded_document_and_sets_a_lease(db_session: Session) -> None:
    newer = add_document(db_session, created_at=T0 - timedelta(minutes=1))
    older = add_document(db_session, created_at=T0 - timedelta(minutes=5))

    claimed = claim(db_session)

    assert claimed is not None and claimed.id == older.id
    assert claimed.status is DocumentStatus.PROCESSING
    assert claimed.attempts == 1
    assert claimed.lease_expires_at == T0 + timedelta(seconds=LEASE)
    assert newer.status is DocumentStatus.UPLOADED


def test_a_claimed_document_is_not_claimed_twice_while_its_lease_is_valid(
    db_session: Session,
) -> None:
    add_document(db_session)
    assert claim(db_session) is not None

    assert claim(db_session, T0 + timedelta(seconds=LEASE - 1)) is None


def test_ready_and_failed_documents_are_never_claimed(db_session: Session) -> None:
    ready = add_document(db_session)
    ready.transition_to(DocumentStatus.PROCESSING)
    ready.transition_to(DocumentStatus.READY)
    failed = add_document(db_session)
    failed.transition_to(DocumentStatus.PROCESSING)
    failed.transition_to(DocumentStatus.FAILED, error_summary="roto")

    assert claim(db_session) is None


# ── Recuperación tras una caída ─────────────────────────────────────────────


def test_a_crashed_worker_leaves_the_document_recoverable(db_session: Session) -> None:
    document = add_document(db_session)
    assert claim(db_session) is not None  # el worker reserva… y muere sin terminar

    recovered = claim(db_session, T0 + timedelta(seconds=LEASE + 1))

    assert recovered is not None and recovered.id == document.id
    assert recovered.status is DocumentStatus.PROCESSING
    assert recovered.attempts == 2
    assert recovered.lease_expires_at == T0 + timedelta(seconds=2 * LEASE + 1)


def test_a_processing_document_without_a_lease_is_recoverable(db_session: Session) -> None:
    document = add_document(db_session)
    document.transition_to(DocumentStatus.PROCESSING)  # sin arrendamiento (caso heredado)

    recovered = claim(db_session)

    assert recovered is not None and recovered.attempts == 2


def test_a_document_that_keeps_crashing_ends_failed_not_in_a_loop(db_session: Session) -> None:
    document = add_document(db_session)
    now = T0
    for _ in range(MAX_ATTEMPTS):
        assert claim(db_session, now) is not None  # cada intento muere sin terminar
        now += timedelta(seconds=LEASE + 1)

    assert claim(db_session, now) is None

    db_session.refresh(document)
    assert document.status is DocumentStatus.FAILED
    assert document.attempts == MAX_ATTEMPTS
    assert document.error_summary == service.INTERRUPTED
    assert document.lease_expires_at is None


# ── Procesamiento ───────────────────────────────────────────────────────────


def test_processing_a_valid_document_makes_it_ready(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = add_document(db_session, storage, make_pdf(["Hola", "Mundo"]))

    assert run_once(
        db_session,
        storage,
        policy=POLICY,
        embedder=EMBEDDER,
        lease_seconds=LEASE,
        max_attempts=MAX_ATTEMPTS,
        now=T0,
    )

    assert document.status is DocumentStatus.READY
    assert document.error_summary is None
    assert document.lease_expires_at is None
    assert document.processed_at is not None


def test_a_document_without_text_ends_failed_with_its_cause(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = add_document(db_session, storage, make_pdf([None]))

    run_once(
        db_session,
        storage,
        policy=POLICY,
        embedder=EMBEDDER,
        lease_seconds=LEASE,
        max_attempts=MAX_ATTEMPTS,
        now=T0,
    )

    assert document.status is DocumentStatus.FAILED
    assert document.error_summary is not None
    assert "no contiene texto extraíble" in document.error_summary


def test_run_once_reports_when_there_was_nothing_to_do(
    db_session: Session, storage: LocalFileStorage
) -> None:
    assert not run_once(
        db_session,
        storage,
        policy=POLICY,
        embedder=EMBEDDER,
        lease_seconds=LEASE,
        max_attempts=MAX_ATTEMPTS,
    )


def test_an_unexpected_error_requeues_the_document_for_retry(
    db_session: Session, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(service, "extract_or_fail", boom)
    document = add_document(db_session, storage, b"hola")

    run_once(
        db_session,
        storage,
        policy=POLICY,
        embedder=EMBEDDER,
        lease_seconds=LEASE,
        max_attempts=MAX_ATTEMPTS,
        now=T0,
    )

    assert document.status is DocumentStatus.UPLOADED
    assert document.error_summary == service.UNEXPECTED_RETRY
    assert document.attempts == 1
    assert document.lease_expires_at is None


def test_unexpected_errors_end_failed_once_attempts_run_out(
    db_session: Session, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(service, "extract_or_fail", boom)
    document = add_document(db_session, storage, b"hola")

    for _ in range(MAX_ATTEMPTS):
        assert run_once(
            db_session,
            storage,
            policy=POLICY,
            embedder=EMBEDDER,
            lease_seconds=LEASE,
            max_attempts=MAX_ATTEMPTS,
            now=T0,
        )

    assert document.status is DocumentStatus.FAILED
    assert document.attempts == MAX_ATTEMPTS
    assert document.error_summary == service.UNEXPECTED_FAILURE
    assert not run_once(
        db_session,
        storage,
        policy=POLICY,
        embedder=EMBEDDER,
        lease_seconds=LEASE,
        max_attempts=MAX_ATTEMPTS,
        now=T0,
    )


def test_process_keeps_a_failed_extraction_failed(
    db_session: Session, storage: LocalFileStorage
) -> None:
    add_document(db_session, storage, None)
    document = claim(db_session)
    assert document is not None

    process(
        db_session, storage, document, policy=POLICY, embedder=EMBEDDER, max_attempts=MAX_ATTEMPTS
    )  # sin archivo almacenado

    assert document.status is DocumentStatus.FAILED
    assert document.error_summary == "No se encontró el archivo original."


# ── Concurrencia (dos sesiones reales) ──────────────────────────────────────


def test_two_workers_never_claim_the_same_document(db_engine: Engine) -> None:
    with Session(db_engine) as setup:
        user = add_user(setup)
        setup.add_all([make_document(user.id), make_document(user.id)])
        setup.commit()
        user_id = user.id
    try:
        with Session(db_engine) as first, Session(db_engine) as second:
            # Sin commit entre ambas: la segunda debe saltarse la fila bloqueada por la primera.
            a = first.scalars(service.select_claimable(T0)).first()
            b = second.scalars(service.select_claimable(T0)).first()
            assert a is not None and b is not None
            assert a.id != b.id
            assert second.scalars(service.select_claimable(T0)).first() is not None
            first.rollback()
            second.rollback()
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


# ── Bucle del worker ────────────────────────────────────────────────────────


def test_worker_loop_processes_pending_documents_and_stops_on_request(
    db_engine: Engine, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr("app.ingestion.worker.get_sessionmaker", lambda: sessionmaker(db_engine))
    monkeypatch.setattr("app.ingestion.worker.get_storage", lambda: storage)
    monkeypatch.setenv("INGESTION_POLL_SECONDS", "0.05")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with Session(db_engine) as setup:
        user = add_user(setup)
        document = make_document(user.id, filename="nota.txt", content_type="text/plain")
        setup.add(document)
        setup.flush()
        storage.save(document.storage_key, io.BytesIO(b"contenido de prueba"))
        setup.commit()
        document_id, user_id = document.id, user.id

    stop = threading.Event()
    thread = threading.Thread(target=run, args=(stop,))
    try:
        thread.start()
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        status = DocumentStatus.UPLOADED
        while status is not DocumentStatus.READY and datetime.now(UTC) < deadline:
            with Session(db_engine) as check:
                status = check.scalars(
                    select(Document.status).where(Document.id == document_id)
                ).one()
            stop.wait(0.05)
        assert status is DocumentStatus.READY
    finally:
        stop.set()
        thread.join(timeout=5)
        get_settings.cache_clear()
        with Session(db_engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()

    assert not thread.is_alive()
