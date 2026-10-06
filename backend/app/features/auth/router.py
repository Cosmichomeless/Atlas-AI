from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.errors import AppError, error_responses
from app.core.db import get_session
from app.features.auth.schemas import RegisterRequest, UserPublic
from app.features.users.service import EmailAlreadyRegisteredError, create_user

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/register",
    status_code=201,
    summary="Registrar una cuenta",
    responses=error_responses(409),
)
def register(payload: RegisterRequest, session: SessionDep) -> UserPublic:
    try:
        user = create_user(session, payload.email, payload.password)
    except EmailAlreadyRegisteredError:
        raise AppError(
            409, "email_already_registered", "Ya existe una cuenta con ese email."
        ) from None
    return UserPublic.model_validate(user)
