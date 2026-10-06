import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.auth.cookies import SESSION_COOKIE
from app.features.auth.models import AuthSession
from app.features.auth.service import hash_token
from app.features.documents.models import Document

CREDENTIALS = {"email": "ana@example.com", "password": "correct horse battery"}


def register(client: TestClient, **overrides: str) -> dict[str, str]:
    payload = {**CREDENTIALS, **overrides}
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    return dict(response.json())


def test_valid_login_creates_session_and_cookie(client: TestClient, db_session: Session) -> None:
    register(client)

    response = client.post("/api/v1/auth/login", json=CREDENTIALS)

    assert response.status_code == 200
    assert response.json()["email"] == "ana@example.com"
    token = client.cookies[SESSION_COOKIE]
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header
    stored = db_session.scalars(select(AuthSession)).one()
    assert stored.token_hash == hash_token(token) != token  # el token no se guarda en claro
    assert client.get("/api/v1/auth/me").json()["email"] == "ana@example.com"


def test_logout_invalidates_the_session(client: TestClient, db_session: Session) -> None:
    register(client)
    client.post("/api/v1/auth/login", json=CREDENTIALS)
    stolen = client.cookies[SESSION_COOKIE]

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert db_session.scalars(select(AuthSession)).all() == []
    client.cookies.set(SESSION_COOKIE, stolen)  # reutilizar el token anterior ya no sirve
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_is_idempotent_for_anonymous_requests(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_anonymous_requests_to_protected_routes_return_401(client: TestClient) -> None:
    for path in ("/api/v1/documents", "/api/v1/auth/me"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_unknown_session_token_returns_401(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged-token")

    assert client.get("/api/v1/documents").status_code == 401


def test_expired_session_returns_401(client: TestClient, db_session: Session) -> None:
    register(client)
    client.post("/api/v1/auth/login", json=CREDENTIALS)
    stored = db_session.scalars(select(AuthSession)).one()
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_wrong_password_and_unknown_email_are_indistinguishable(client: TestClient) -> None:
    register(client)

    wrong = client.post("/api/v1/auth/login", json={**CREDENTIALS, "password": "wrong-password!"})
    unknown = client.post("/api/v1/auth/login", json={**CREDENTIALS, "email": "no@example.com"})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_credentials"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert SESSION_COOKIE not in client.cookies


def test_login_normalises_email_and_issues_a_new_token_each_time(client: TestClient) -> None:
    register(client)

    client.post("/api/v1/auth/login", json={**CREDENTIALS, "email": " ANA@example.com"})
    first = client.cookies[SESSION_COOKIE]
    client.post("/api/v1/auth/login", json=CREDENTIALS)

    assert client.cookies[SESSION_COOKIE] != first


def test_documents_are_scoped_to_the_logged_in_user(
    client: TestClient, db_session: Session
) -> None:
    ana = register(client)
    ben = register(client, email="ben@example.com")
    db_session.add_all(
        [Document(owner_id=uuid.UUID(ana["id"])), Document(owner_id=uuid.UUID(ben["id"]))]
    )
    db_session.flush()

    client.post("/api/v1/auth/login", json=CREDENTIALS)
    items = client.get("/api/v1/documents").json()["items"]

    assert len(items) == 1
