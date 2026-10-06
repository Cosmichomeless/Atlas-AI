import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.features.documents import queries
from app.features.documents.models import Document
from app.features.users.models import User
from tests.factories import make_document


def make_user(session: Session, email: str) -> User:
    user = User(email=email, password_hash="not-a-real-hash")
    session.add(user)
    session.flush()
    return user


def test_user_gets_uuid_and_creation_date(db_session: Session) -> None:
    user = make_user(db_session, "ana@example.com")
    db_session.refresh(user)

    assert isinstance(user.id, uuid.UUID)
    assert user.created_at is not None


def test_email_is_unique(db_session: Session) -> None:
    make_user(db_session, "ana@example.com")

    with pytest.raises(IntegrityError):
        make_user(db_session, "ana@example.com")


def test_email_uniqueness_ignores_case(db_session: Session) -> None:
    make_user(db_session, "ana@example.com")

    # La base rechaza emails no normalizados, así que "ANA@..." nunca puede coexistir con "ana@...".
    with pytest.raises(IntegrityError):
        make_user(db_session, "ANA@example.com")


def test_document_requires_owner(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        db_session.add(make_document(None))
        db_session.flush()


def test_document_owner_must_exist(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        db_session.add(make_document(uuid.uuid4()))
        db_session.flush()


def test_deleting_user_deletes_their_documents(db_session: Session) -> None:
    user = make_user(db_session, "ana@example.com")
    db_session.add(make_document(user.id))
    db_session.flush()

    db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})

    assert db_session.scalars(select(Document)).all() == []


def test_queries_only_return_the_owners_documents(db_session: Session) -> None:
    ana = make_user(db_session, "ana@example.com")
    ben = make_user(db_session, "ben@example.com")
    mine = make_document(ana.id)
    theirs = make_document(ben.id)
    db_session.add_all([mine, theirs])
    db_session.flush()

    assert [d.id for d in queries.list_owned(db_session, ana.id)] == [mine.id]
    assert queries.get_owned(db_session, ana.id, mine.id) is not None


def test_foreign_and_missing_documents_are_indistinguishable(db_session: Session) -> None:
    ana = make_user(db_session, "ana@example.com")
    ben = make_user(db_session, "ben@example.com")
    theirs = make_document(ben.id)
    db_session.add(theirs)
    db_session.flush()

    assert queries.get_owned(db_session, ana.id, theirs.id) is None
    assert queries.get_owned(db_session, ana.id, uuid.uuid4()) is None


def test_user_repr_does_not_leak_the_hash(db_session: Session) -> None:
    user = make_user(db_session, "ana@example.com")

    assert "not-a-real-hash" not in repr(user)
