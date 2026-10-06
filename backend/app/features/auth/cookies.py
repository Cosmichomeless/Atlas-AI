from fastapi import Response

from app.api.csrf import CSRF_COOKIE
from app.core.config import get_settings

SESSION_COOKIE = "atlas_session"


def _flags() -> dict[str, object]:
    """Atributos comunes de nuestras cookies; todas HttpOnly, SameSite y Secure según entorno."""
    settings = get_settings()
    return {
        "httponly": True,
        "samesite": settings.cookie_samesite,
        "secure": settings.session_cookie_secure,
        "path": "/",
    }


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=get_settings().session_ttl_hours * 3600,
        **_flags(),  # type: ignore[arg-type]
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, **_flags())  # type: ignore[arg-type]


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=get_settings().session_ttl_hours * 3600,
        **_flags(),  # type: ignore[arg-type]
    )
