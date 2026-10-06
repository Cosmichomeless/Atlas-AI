"""Defensa CSRF para el API con cookies de sesión.

Doble envío firmado: el servidor emite un token ``nonce.firma`` (HMAC-SHA256 con ``SECRET_KEY``)
y lo entrega a la vez en una cookie HttpOnly y en el cuerpo de la respuesta. El frontend lo guarda
en memoria y lo reenvía en la cabecera ``X-CSRF-Token``. Una operación mutable solo se acepta si:

1. la cabecera coincide con la cookie (un sitio ajeno no puede leer ni fijar la cookie de nuestro
   origen) y la firma es válida (no se puede fabricar un par propio), y
2. si el navegador envía ``Origin``, es exactamente ``FRONTEND_ORIGIN``.
"""

import hashlib
import hmac
import secrets
from functools import lru_cache
from typing import Annotated

from fastapi import Cookie, Header, Request

from app.api.errors import AppError
from app.core.config import get_settings

CSRF_COOKIE = "atlas_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@lru_cache
def _ephemeral_key() -> bytes:
    # Sin SECRET_KEY (solo desarrollo/tests): clave aleatoria por proceso.
    return secrets.token_bytes(32)


def _key() -> bytes:
    secret = get_settings().secret_key
    return secret.get_secret_value().encode() if secret else _ephemeral_key()


def _sign(nonce: str) -> str:
    return hmac.new(_key(), nonce.encode(), hashlib.sha256).hexdigest()


def generate_token() -> str:
    nonce = secrets.token_urlsafe(24)
    return f"{nonce}.{_sign(nonce)}"


def is_valid_token(token: str) -> bool:
    nonce, _, signature = token.partition(".")
    return bool(nonce) and hmac.compare_digest(signature, _sign(nonce))


def verify_csrf(
    request: Request,
    header_token: Annotated[str | None, Header(alias=CSRF_HEADER, include_in_schema=False)] = None,
    cookie_token: Annotated[str | None, Cookie(alias=CSRF_COOKIE, include_in_schema=False)] = None,
) -> None:
    """Dependencia aplicada al router de la API: exige defensa CSRF en métodos mutables."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin != get_settings().frontend_origin:
        raise AppError(403, "csrf_failed", "Origen no permitido.")
    if not header_token or not cookie_token:
        raise AppError(403, "csrf_failed", "Falta el token CSRF.")
    if not hmac.compare_digest(header_token, cookie_token) or not is_valid_token(header_token):
        raise AppError(403, "csrf_failed", "Token CSRF no válido.")
