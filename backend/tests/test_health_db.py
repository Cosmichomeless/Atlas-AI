import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core import config, db
from app.main import app
from tests.conftest import TEST_DATABASE_URL


def test_pgvector_extension_enabled() -> None:
    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        ).one()
        distance = conn.execute(text("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")).scalar_one()
    assert distance == 1.0


def test_health_db_ok() -> None:
    response = TestClient(app).get("/api/v1/health/db")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["pgvector_version"]


def test_health_db_unavailable_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        response = TestClient(app).get("/api/v1/health/db")
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()
        db.get_engine.cache_clear()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
