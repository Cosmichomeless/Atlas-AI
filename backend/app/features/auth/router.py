from typing import Annotated

from fastapi import APIRouter, Cookie, Response

from app.api.csrf import CSRF_COOKIE, generate_token, is_valid_token
from app.api.errors import AppError, error_responses
from app.features.auth.cookies import (
    SESSION_COOKIE,
    clear_session_cookie,
    set_csrf_cookie,
    set_session_cookie,
)
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.auth.schemas import CsrfToken, LoginRequest, RegisterRequest, UserPublic
from app.features.auth.service import authenticate, create_session, delete_session
from app.features.users.service import EmailAlreadyRegisteredError, create_user

router = APIRouter(prefix="/auth", tags=["auth"])


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


@router.post(
    "/login",
    summary="Iniciar sesión",
    description="Crea una sesión y la entrega en una cookie HttpOnly.",
    responses=error_responses(401),
)
def login(payload: LoginRequest, session: SessionDep, response: Response) -> UserPublic:
    user = authenticate(session, payload.email, payload.password)
    if user is None:
        # Mismo error para email desconocido y contraseña incorrecta.
        raise AppError(401, "invalid_credentials", "Email o contraseña incorrectos.")
    set_session_cookie(response, create_session(session, user))
    return UserPublic.model_validate(user)


@router.post(
    "/logout",
    status_code=204,
    summary="Cerrar sesión",
    description="Invalida la sesión actual (si existe) y borra la cookie. Es idempotente.",
)
def logout(
    session: SessionDep,
    response: Response,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE, include_in_schema=False)] = None,
) -> None:
    if token:
        delete_session(session, token)
    clear_session_cookie(response)


@router.get(
    "/me",
    summary="Usuario actual",
    responses=error_responses(401),
)
def me(user: CurrentUser) -> UserPublic:
    return UserPublic.model_validate(user)


@router.get(
    "/csrf",
    summary="Obtener token CSRF",
    description=(
        "Emite (o reutiliza) un token CSRF firmado: lo fija en una cookie HttpOnly y lo devuelve "
        "para que el cliente lo envíe en `X-CSRF-Token` en las operaciones mutables."
    ),
)
def csrf(
    response: Response,
    cookie_token: Annotated[str | None, Cookie(alias=CSRF_COOKIE, include_in_schema=False)] = None,
) -> CsrfToken:
    token = cookie_token if cookie_token and is_valid_token(cookie_token) else generate_token()
    set_csrf_cookie(response, token)
    return CsrfToken(csrf_token=token)
