"""`python -m app.reconcile` detecta documentos sin original y archivos sin documento."""

import io
import uuid

import pytest
from sqlalchemy.orm import Session

from app import reconcile
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage, document_key
from app.features.users.models import User


def add_document(
    session: Session, storage: LocalFileStorage, owner: User, name: str, *, with_file: bool
) -> Document:
    document_id = uuid.uuid4()
    key = document_key(owner.id, document_id)
    document = Document(
        id=document_id,
        owner_id=owner.id,
        filename=name,
        content_type="text/plain",
        size_bytes=3,
        storage_key=key,
        status=DocumentStatus.UPLOADED,
    )
    session.add(document)
    session.flush()
    document.status = DocumentStatus.PROCESSING
    document.status = DocumentStatus.READY
    session.flush()
    if with_file:
        storage.save(key, io.BytesIO(b"abc"))
    return document


@pytest.fixture
def owner(db_session: Session) -> User:
    user = User(email="ana@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()
    return user


def test_consistent_data_reports_nothing_to_fix(
    db_session: Session, storage: LocalFileStorage, owner: User
) -> None:
    add_document(db_session, storage, owner, "a.txt", with_file=True)
    add_document(db_session, storage, owner, "b.txt", with_file=True)

    report = reconcile.reconcile(db_session, storage)

    assert report.consistent
    assert (report.documents, report.files) == (2, 2)
    assert reconcile.render(report).endswith("Todo cuadra.")


def test_a_document_without_its_original_is_reported(
    db_session: Session, storage: LocalFileStorage, owner: User
) -> None:
    add_document(db_session, storage, owner, "ok.txt", with_file=True)
    lost = add_document(db_session, storage, owner, "perdido.txt", with_file=False)

    report = reconcile.reconcile(db_session, storage)

    assert not report.consistent
    assert [m.document_id for m in report.missing_originals] == [str(lost.id)]
    assert report.missing_originals[0].filename == "perdido.txt"
    assert "Original ausente (1)" in reconcile.render(report)


def test_a_file_without_a_document_is_reported_but_upload_temporaries_are_ignored(
    db_session: Session, storage: LocalFileStorage, owner: User
) -> None:
    add_document(db_session, storage, owner, "ok.txt", with_file=True)
    orphan = document_key(owner.id, uuid.uuid4())
    storage.save(orphan, io.BytesIO(b"x"))
    temporary = storage.root / str(owner.id) / ".upload-abc123"
    temporary.write_bytes(b"parcial")

    report = reconcile.reconcile(db_session, storage)

    assert report.orphan_files == [orphan]
    assert not report.missing_originals


def test_a_missing_storage_directory_means_every_document_lacks_its_original(
    db_session: Session, tmp_path: pytest.TempPathFactory, owner: User
) -> None:
    storage = LocalFileStorage(tmp_path / "nunca-creado")  # type: ignore[operator]
    add_document(db_session, storage, owner, "a.txt", with_file=False)

    report = reconcile.reconcile(db_session, storage)

    assert len(report.missing_originals) == 1
    assert report.files == 0
