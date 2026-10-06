from typing import Annotated

from fastapi import Cookie, Depends
from sqlalchemy.orm import Session

from app.api.errors import AppError
from app.core.db import get_session
from app.features.auth.cookies import SESSION_COOKIE
from app.features.auth.service import get_user_for_token
from app.features.users.models import User

SessionDep = Annotated[Session, Depends(get_session)]


def get_current_user(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE, include_in_schema=False)] = None,
) -> User:
    """Usuario de la sesión actual; 401 si la petición es anónima o la sesión no es válida."""
    user = get_user_for_token(session, token) if token else None
    if user is None:
        raise AppError(401, "unauthorized", "Inicia sesión para continuar.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
