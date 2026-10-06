import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import AppError
from app.main import create_app
from app.openapi_export import OPENAPI_PATH, render_openapi


@pytest.fixture
def app() -> FastAPI:
    app = create_app()

    @app.get("/api/v1/_probe/items/{item_id}")
    def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/api/v1/_probe/conflict")
    def conflict() -> None:
        raise AppError(409, "duplicate", "Ya existe un recurso con ese nombre.")

    @app.get("/api/v1/_probe/boom")
    def boom() -> None:
        raise RuntimeError("secreto interno: password=hunter2")

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def error_of(response: Any) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    error: dict[str, Any] = body["error"]
    assert {"code", "message", "request_id", "details"} <= set(error)
    return error


def test_not_found_uses_common_error_format(client: TestClient) -> None:
    response = client.get("/api/v1/no-existe")

    assert response.status_code == 404
    error = error_of(response)
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_method_not_allowed(client: TestClient) -> None:
    response = client.post("/api/v1/health")

    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"
    assert "allow" in response.headers  # se conservan las cabeceras de la excepción


def test_validation_error_lists_field_details(client: TestClient) -> None:
    response = client.get("/api/v1/_probe/items/abc")

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["path", "item_id"]
    assert error["details"][0]["message"]


def test_app_error_carries_its_status_and_code(client: TestClient) -> None:
    response = client.get("/api/v1/_probe/conflict")

    assert response.status_code == 409
    error = error_of(response)
    assert (error["code"], error["message"]) == (
        "duplicate",
        "Ya existe un recurso con ese nombre.",
    )


def test_unhandled_exception_returns_500_without_leaking_details(client: TestClient) -> None:
    response = client.get("/api/v1/_probe/boom")

    assert response.status_code == 500
    error = error_of(response)
    assert error["code"] == "internal_error"
    assert "hunter2" not in response.text


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    generated = client.get("/api/v1/health").headers["X-Request-ID"]
    assert len(generated) == 32

    echoed = client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["X-Request-ID"] == "abc-123"

    unsafe = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert unsafe.headers["X-Request-ID"] != "bad id\twith spaces"


def test_cors_only_allows_configured_origin(client: TestClient) -> None:
    allowed = client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    denied = client.options(
        "/api/v1/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_openapi_describes_health_and_common_errors() -> None:
    schema = TestClient(create_app()).get("/openapi.json").json()
    paths = schema["paths"]
    error_ref = {"$ref": "#/components/schemas/ErrorResponse"}

    health = paths["/api/v1/health"]["get"]["responses"]
    assert "200" in health
    for status in ("422", "500"):
        assert health[status]["content"]["application/json"]["schema"] == error_ref

    health_db = paths["/api/v1/health/db"]["get"]["responses"]
    assert health_db["503"]["content"]["application/json"]["schema"] == error_ref

    components = schema["components"]["schemas"]
    assert {"ErrorResponse", "ErrorBody", "ErrorDetail"} <= set(components)
    assert "HTTPValidationError" not in components


def test_committed_openapi_contract_is_up_to_date() -> None:
    """Si falla: `uv run python -m app.openapi_export` y regenera el cliente del frontend."""
    committed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert committed == json.loads(render_openapi())
