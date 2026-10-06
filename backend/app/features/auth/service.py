import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password, password_needs_rehash, verify_password
from app.features.auth.models import AuthSession
from app.features.users.models import User
from app.features.users.service import get_user_by_email


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@lru_cache
def _dummy_hash() -> str:
    return hash_password("atlas-dummy-password")


def authenticate(session: Session, email: str, password: str) -> User | None:
    """Comprueba credenciales. Con un email desconocido hace igualmente una verificación Argon2
    para que el tiempo de respuesta no revele si la cuenta existe."""
    user = get_user_by_email(session, email)
    if user is None:
        verify_password(_dummy_hash(), password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        session.commit()
    return user


def create_session(session: Session, user: User) -> str:
    """Crea una sesión nueva (token nuevo en cada login) y devuelve el token en claro."""
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    session.add(
        AuthSession(
            user_id=user.id, token_hash=hash_token(token), expires_at=datetime.now(UTC) + ttl
        )
    )
    session.commit()
    return token


def get_user_for_token(session: Session, token: str) -> User | None:
    return session.scalars(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            AuthSession.token_hash == hash_token(token), AuthSession.expires_at > datetime.now(UTC)
        )
    ).first()


def delete_session(session: Session, token: str) -> None:
    session.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    session.commit()
