"""Frontend y API en sitios distintos (p. ej. Vercel + una API propia detrás de un túnel HTTPS).

Es la configuración que documenta `docs/access-and-secrets.md`: `FRONTEND_ORIGIN` es el dominio del
frontend, y las cookies son `SameSite=None; Secure`. Aquí se comprueba que CORS, CSRF y las cookies
siguen cerrando el paso a cualquier otro origen.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.features.auth.cookies import SESSION_COOKIE
from app.features.documents.storage import FileStorage, get_storage
from app.main import create_app

FRONTEND = "https://atlas-ai-one-silk.vercel.app"
ACCOUNT = {"email": "ana@example.com", "password": "correct horse battery"}


@pytest.fixture
def cross_site(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, storage: FileStorage
) -> Iterator[TestClient]:
    monkeypatch.setenv("FRONTEND_ORIGIN", FRONTEND)
    monkeypatch.setenv("COOKIE_SAMESITE", "none")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage

    def override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = override
    try:
        # https: las cookies Secure solo vuelven al servidor por una conexión segura.
        with TestClient(app, base_url="https://api.example.test") as test_client:
            yield test_client
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _cookie(response_headers: list[tuple[str, str]], name: str) -> str:
    return next(v for k, v in response_headers if k == "set-cookie" and name in v).lower()


def test_cors_allows_only_the_frontend_domain_and_credentials(cross_site: TestClient) -> None:
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-csrf-token",
    }
    allowed = cross_site.options("/api/v1/auth/login", headers={"Origin": FRONTEND, **headers})
    denied = cross_site.options(
        "/api/v1/auth/login", headers={"Origin": "http://localhost:3000", **headers}
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == FRONTEND
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in denied.headers


def test_cookies_are_samesite_none_secure_and_httponly(cross_site: TestClient) -> None:
    csrf = cross_site.get("/api/v1/auth/csrf")
    token = csrf.json()["csrf_token"]
    headers = {"Origin": FRONTEND, "X-CSRF-Token": token}
    assert (
        cross_site.post("/api/v1/auth/register", json=ACCOUNT, headers=headers).status_code == 201
    )
    login = cross_site.post("/api/v1/auth/login", json=ACCOUNT, headers=headers)

    assert login.status_code == 200
    for response, name in ((csrf, "atlas_csrf"), (login, SESSION_COOKIE)):
        cookie = _cookie(response.headers.multi_items(), name)
        assert "samesite=none" in cookie
        assert "secure" in cookie.replace("samesite", "")
        assert "httponly" in cookie


def test_a_request_from_another_origin_is_rejected_even_with_a_valid_token(
    cross_site: TestClient,
) -> None:
    token = cross_site.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = cross_site.post(
        "/api/v1/auth/register",
        json=ACCOUNT,
        headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
    )

    assert response.status_code == 403
