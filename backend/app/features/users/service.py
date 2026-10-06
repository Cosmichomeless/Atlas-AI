from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.features.users.models import User


class EmailAlreadyRegisteredError(Exception):
    """Ya existe una cuenta con ese email."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalars(select(User).where(User.email == normalize_email(email))).first()


def create_user(session: Session, email: str, password: str) -> User:
    """Crea la cuenta guardando solo el hash. La restricción única decide ante carreras."""
    email = normalize_email(email)
    if get_user_by_email(session, email) is not None:
        raise EmailAlreadyRegisteredError(email)
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise EmailAlreadyRegisteredError(email) from exc
    return user
