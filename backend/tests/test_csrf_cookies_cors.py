import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.csrf import CSRF_COOKIE, generate_token, is_valid_token
from app.core.config import Settings, get_settings
from app.features.auth.cookies import SESSION_COOKIE

CSRF = "/api/v1/auth/csrf"
REGISTER = "/api/v1/auth/register"
ACCOUNT = {"email": "ana@example.com", "password": "correct horse battery"}
ORIGIN = "http://localhost:3000"


def test_csrf_endpoint_sets_http_only_cookie_and_returns_token(raw_client: TestClient) -> None:
    response = raw_client.get(CSRF)

    token = response.json()["csrf_token"]
    assert is_valid_token(token)
    assert raw_client.cookies[CSRF_COOKIE] == token
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie.replace("samesite", "")  # entorno de test: sin Secure
    assert raw_client.get(CSRF).json()["csrf_token"] == token  # se reutiliza el vigente


def test_mutation_without_token_is_rejected(raw_client: TestClient) -> None:
    response = raw_client.post(REGISTER, json=ACCOUNT)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_csrf_beats_validation_errors(raw_client: TestClient) -> None:
    assert raw_client.post(REGISTER, json={}).status_code == 403


def test_header_without_cookie_is_rejected(raw_client: TestClient) -> None:
    response = raw_client.post(REGISTER, json=ACCOUNT, headers={"X-CSRF-Token": generate_token()})

    assert response.status_code == 403


def test_mismatched_header_and_cookie_is_rejected(raw_client: TestClient) -> None:
    raw_client.get(CSRF)
    response = raw_client.post(REGISTER, json=ACCOUNT, headers={"X-CSRF-Token": generate_token()})

    assert response.status_code == 403


def test_forged_token_is_rejected_even_if_header_matches_cookie(raw_client: TestClient) -> None:
    forged = "attacker-nonce.deadbeef"
    raw_client.cookies.set(CSRF_COOKIE, forged)
    response = raw_client.post(REGISTER, json=ACCOUNT, headers={"X-CSRF-Token": forged})

    assert response.status_code == 403


def test_valid_token_allows_mutation(client: TestClient) -> None:
    assert client.post(REGISTER, json=ACCOUNT).status_code == 201


def test_logout_and_login_also_require_csrf(raw_client: TestClient) -> None:
    assert raw_client.post("/api/v1/auth/logout").status_code == 403
    assert raw_client.post("/api/v1/auth/login", json=ACCOUNT).status_code == 403


def test_safe_methods_do_not_need_csrf(raw_client: TestClient) -> None:
    assert raw_client.get("/api/v1/health").status_code == 200


def test_foreign_origin_is_rejected_even_with_valid_token(client: TestClient) -> None:
    response = client.post(REGISTER, json=ACCOUNT, headers={"Origin": "http://evil.example"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_configured_origin_is_accepted(client: TestClient) -> None:
    assert client.post(REGISTER, json=ACCOUNT, headers={"Origin": ORIGIN}).status_code == 201


def test_cors_preflight_for_mutations_allows_csrf_header_only_for_configured_origin(
    raw_client: TestClient,
) -> None:
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-csrf-token",
    }
    allowed = raw_client.options(REGISTER, headers={"Origin": ORIGIN, **headers})
    denied = raw_client.options(REGISTER, headers={"Origin": "http://evil.example", **headers})

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "x-csrf-token" in allowed.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    ("env", "expected_secure"), [("development", False), ("test", False), ("production", True)]
)
def test_session_cookie_flags_follow_environment(env: str, expected_secure: bool) -> None:
    settings = Settings(_env_file=None, app_env=env, secret_key=SecretStr("s" * 32))  # type: ignore[call-arg, arg-type]
    assert settings.session_cookie_secure is expected_secure


def test_login_sets_http_only_samesite_cookie(client: TestClient) -> None:
    client.post(REGISTER, json=ACCOUNT)
    response = client.post("/api/v1/auth/login", json=ACCOUNT)

    cookie = next(
        v for k, v in response.headers.multi_items() if k == "set-cookie" and SESSION_COOKIE in v
    ).lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_production_cookies_are_secure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "s" * 32)
    get_settings.cache_clear()
    try:
        response = client.get(CSRF)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert "secure" in response.headers["set-cookie"].lower().replace("samesite", "")


def test_samesite_none_requires_secure_cookies() -> None:
    with pytest.raises(ValueError, match="Secure"):
        Settings(_env_file=None, cookie_samesite="none", cookie_secure=False)  # type: ignore[call-arg]
    ok = Settings(_env_file=None, cookie_samesite="none", cookie_secure=True)  # type: ignore[call-arg]
    assert ok.session_cookie_secure is True


def test_production_refuses_insecure_cookies() -> None:
    with pytest.raises(ValueError, match="Secure"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="production",
            secret_key=SecretStr("s" * 32),
            cookie_secure=False,
        )
