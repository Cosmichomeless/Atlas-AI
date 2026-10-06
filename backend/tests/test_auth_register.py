import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password, password_needs_rehash, verify_password
from app.features.users import service
from app.features.users.models import User

URL = "/api/v1/auth/register"
VALID = {"email": "ana@example.com", "password": "correct horse battery"}


def test_valid_registration_creates_account_without_exposing_hash(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(URL, json=VALID)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "created_at"}
    assert body["email"] == "ana@example.com"
    assert "password" not in response.text and "argon2" not in response.text
    stored = db_session.get(User, body["id"])
    assert stored is not None
    assert stored.password_hash.startswith("$argon2id$")
    assert VALID["password"] not in stored.password_hash


def test_email_is_normalised(client: TestClient) -> None:
    response = client.post(URL, json={**VALID, "email": "  Ana@Example.COM "})

    assert response.status_code == 201
    assert response.json()["email"] == "ana@example.com"


def test_duplicate_email_returns_conflict_regardless_of_case(client: TestClient) -> None:
    assert client.post(URL, json=VALID).status_code == 201

    response = client.post(URL, json={**VALID, "email": "ANA@example.com"})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "email_already_registered"
    assert error["message"]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({**VALID, "email": "not-an-email"}, "email"),
        ({**VALID, "email": ""}, "email"),
        ({**VALID, "password": "short"}, "password"),
        ({**VALID, "password": " " * 12}, "password"),
        ({**VALID, "password": "x" * 129}, "password"),
        ({"email": VALID["email"]}, "password"),
        ({"password": VALID["password"]}, "email"),
    ],
)
def test_invalid_data_returns_field_errors(
    client: TestClient, payload: dict[str, str], field: str
) -> None:
    response = client.post(URL, json=payload)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert any(d["loc"] == ["body", field] for d in error["details"])


def test_validation_errors_do_not_echo_the_password(client: TestClient) -> None:
    response = client.post(URL, json={**VALID, "password": "tooshort"})

    assert "tooshort" not in response.text


def test_password_hashing_roundtrip() -> None:
    hashed = hash_password("s3cret-passphrase")

    assert verify_password(hashed, "s3cret-passphrase")
    assert not verify_password(hashed, "other")
    assert not verify_password("not-a-hash", "s3cret-passphrase")
    assert not password_needs_rehash(hashed)
    assert hash_password("s3cret-passphrase") != hashed  # sal aleatoria


def test_unique_constraint_decides_concurrent_registrations(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post(URL, json=VALID).status_code == 201
    # Simula que otra petición registró el email entre la comprobación y el insert.
    monkeypatch.setattr(service, "get_user_by_email", lambda session, email: None)

    response = client.post(URL, json=VALID)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"
