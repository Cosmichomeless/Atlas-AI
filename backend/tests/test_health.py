from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_liveness_probe_is_available_but_not_in_contract() -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert "/health" not in client.get("/openapi.json").json()["paths"]


def test_openapi_documents_versioned_health() -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/health/db" in paths
