import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.features.documents.models import ERROR_SUMMARY_MAX_LENGTH, Document
from app.features.documents.states import (
    ALLOWED_TRANSITIONS,
    DocumentStatus,
    InvalidStatusTransitionError,
)
from app.features.users.service import create_user
from tests.factories import make_document

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ALL_PAIRS = list(itertools.product(DocumentStatus, DocumentStatus))
VALID_PAIRS = [(a, b) for a, b in ALL_PAIRS if b in ALLOWED_TRANSITIONS[a]]
INVALID_PAIRS = [(a, b) for a, b in ALL_PAIRS if b not in ALLOWED_TRANSITIONS[a]]


def _doc_in(session: Session, status: DocumentStatus = DocumentStatus.UPLOADED) -> Document:
    """Documento persistido y llevado a `status` por el camino válido más corto."""
    user = create_user(session, f"{uuid.uuid4().hex}@example.com", "correct horse battery")
    document = make_document(user.id)
    session.add(document)
    session.flush()
    path = {
        DocumentStatus.UPLOADED: [],
        DocumentStatus.PROCESSING: [DocumentStatus.PROCESSING],
        DocumentStatus.READY: [DocumentStatus.PROCESSING, DocumentStatus.READY],
        DocumentStatus.FAILED: [DocumentStatus.PROCESSING, DocumentStatus.FAILED],
    }[status]
    for step in path:
        document.transition_to(step, error_summary="boom", now=NOW)
    return document


def test_new_document_stores_metadata_and_starts_uploaded(db_session: Session) -> None:
    document = _doc_in(db_session)
    db_session.refresh(document)

    assert (document.filename, document.content_type, document.size_bytes) == (
        "informe.pdf",
        "application/pdf",
        1024,
    )
    assert document.status is DocumentStatus.UPLOADED
    assert document.attempts == 0
    assert document.created_at is not None and document.updated_at is not None
    assert document.error_summary is None
    assert document.processing_started_at is None and document.processed_at is None


def test_processing_records_start_attempt_and_lease() -> None:
    document = make_document(uuid.uuid4())
    lease = NOW + timedelta(minutes=5)

    document.transition_to(DocumentStatus.PROCESSING, now=NOW, lease_expires_at=lease)

    assert document.status is DocumentStatus.PROCESSING
    assert document.attempts == 1
    assert document.processing_started_at == NOW
    assert document.lease_expires_at == lease


def test_ready_records_completion_and_clears_lease() -> None:
    document = make_document(uuid.uuid4())
    document.transition_to(DocumentStatus.PROCESSING, now=NOW, lease_expires_at=NOW)

    document.transition_to(DocumentStatus.READY, now=NOW + timedelta(seconds=3))

    assert document.processed_at == NOW + timedelta(seconds=3)
    assert document.lease_expires_at is None
    assert document.error_summary is None


def test_failed_records_summary_and_date(db_session: Session) -> None:
    document = _doc_in(db_session, DocumentStatus.PROCESSING)

    document.transition_to(DocumentStatus.FAILED, error_summary="  PDF cifrado  ", now=NOW)
    db_session.flush()
    db_session.refresh(document)

    assert document.status is DocumentStatus.FAILED
    assert document.error_summary == "PDF cifrado"
    assert document.processed_at == NOW


def test_failed_requires_an_error_summary() -> None:
    document = make_document(uuid.uuid4())
    document.transition_to(DocumentStatus.PROCESSING)

    for blank in (None, "", "   "):
        with pytest.raises(ValueError, match="error resumido"):
            document.transition_to(DocumentStatus.FAILED, error_summary=blank)
    assert document.status is DocumentStatus.PROCESSING


def test_error_summary_is_truncated() -> None:
    document = make_document(uuid.uuid4())
    document.transition_to(DocumentStatus.PROCESSING)

    document.transition_to(DocumentStatus.FAILED, error_summary="x" * 5000)

    assert document.error_summary is not None
    assert len(document.error_summary) == ERROR_SUMMARY_MAX_LENGTH


def test_retry_keeps_last_error_until_next_attempt_and_counts_attempts() -> None:
    document = make_document(uuid.uuid4())
    document.transition_to(DocumentStatus.PROCESSING, now=NOW)
    document.transition_to(DocumentStatus.FAILED, error_summary="timeout", now=NOW)

    document.transition_to(DocumentStatus.UPLOADED)
    assert document.error_summary == "timeout"
    assert document.processing_started_at is None and document.processed_at is None

    document.transition_to(DocumentStatus.PROCESSING, now=NOW)
    assert document.attempts == 2
    assert document.error_summary is None


def test_crash_recovery_and_reindex_return_to_uploaded() -> None:
    crashed = make_document(uuid.uuid4())
    crashed.transition_to(DocumentStatus.PROCESSING, now=NOW, lease_expires_at=NOW)
    crashed.transition_to(DocumentStatus.UPLOADED)
    assert crashed.lease_expires_at is None and crashed.processing_started_at is None

    done = make_document(uuid.uuid4())
    done.transition_to(DocumentStatus.PROCESSING)
    done.transition_to(DocumentStatus.READY)
    done.transition_to(DocumentStatus.UPLOADED)
    assert done.processed_at is None


@pytest.mark.parametrize(("origin", "target"), VALID_PAIRS)
def test_every_allowed_transition_works(
    db_session: Session, origin: DocumentStatus, target: DocumentStatus
) -> None:
    document = _doc_in(db_session, origin)

    document.transition_to(target, error_summary="boom")
    db_session.flush()

    assert document.status is target


@pytest.mark.parametrize(("origin", "target"), INVALID_PAIRS)
def test_invalid_transitions_are_rejected_and_change_nothing(
    db_session: Session, origin: DocumentStatus, target: DocumentStatus
) -> None:
    document = _doc_in(db_session, origin)
    attempts = document.attempts

    with pytest.raises(InvalidStatusTransitionError):
        document.transition_to(target, error_summary="boom")

    assert document.status is origin
    assert document.attempts == attempts


def test_assigning_status_directly_is_also_validated() -> None:
    document = make_document(uuid.uuid4())

    with pytest.raises(InvalidStatusTransitionError):
        document.status = DocumentStatus.READY

    assert document.status is DocumentStatus.UPLOADED


def test_a_document_can_only_be_born_uploaded() -> None:
    with pytest.raises(InvalidStatusTransitionError):
        make_document(uuid.uuid4(), status=DocumentStatus.READY)


def test_the_transition_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(DocumentStatus)


def test_updated_at_moves_on_changes(db_session: Session) -> None:
    document = _doc_in(db_session)
    past = NOW - timedelta(days=1)
    db_session.execute(
        text("update documents set updated_at = :past where id = :id"),
        {"past": past, "id": document.id},
    )
    db_session.refresh(document)
    assert document.updated_at == past

    document.filename = "otro.pdf"
    db_session.flush()
    db_session.refresh(document)

    assert document.updated_at > past


def test_database_rejects_unknown_status(db_session: Session) -> None:
    document = _doc_in(db_session)

    with pytest.raises(IntegrityError, match="document_status"):
        db_session.execute(
            text("update documents set status = 'DELETED' where id = :id"), {"id": document.id}
        )


def test_database_rejects_negative_size(db_session: Session) -> None:
    user = create_user(db_session, "neg@example.com", "correct horse battery")
    db_session.add(make_document(user.id, size_bytes=-1))

    with pytest.raises(IntegrityError, match="size_non_negative"):
        db_session.flush()


@pytest.mark.parametrize("field", ["filename", "content_type", "size_bytes"])
def test_metadata_is_required(db_session: Session, field: str) -> None:
    user = create_user(db_session, "req@example.com", "correct horse battery")
    document = make_document(user.id)
    setattr(document, field, None)
    db_session.add(document)

    with pytest.raises(IntegrityError):
        db_session.flush()
