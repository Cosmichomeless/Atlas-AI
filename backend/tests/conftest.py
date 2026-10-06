import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.features.documents.storage import FileStorage, LocalFileStorage, get_storage
from app.main import create_app

# Credenciales de desarrollo local definidas en docker-compose.yml (solo para la base de pruebas).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://atlas:atlas_dev_password@localhost:5433/atlas_test",
)

# La suite nunca debe usar proveedores reales ni la base de desarrollo, aunque exista un `.env`
# con claves: las variables de entorno tienen prioridad sobre ese archivo.
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "EMBEDDING_PROVIDER": "fake",
        "LLM_PROVIDER": "fake",
        "OPENAI_API_KEY": "",
    }
)


BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Engine de `atlas_test` con el esquema migrado a `head` (las migraciones mandan)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Sesión aislada: todo lo que haga el test (incluso commit) se revierte al terminar."""
    connection = db_engine.connect()
    outer = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    """Almacenamiento en un directorio temporal: los tests nunca escriben en `storage/`."""
    return LocalFileStorage(tmp_path / "storage")


@pytest.fixture
def raw_client(db_session: Session, storage: FileStorage) -> Iterator[TestClient]:
    """Cliente sin credenciales CSRF: sirve para probar la defensa CSRF en sí."""
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage

    def override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(raw_client: TestClient) -> TestClient:
    """Cliente que se comporta como el frontend: obtiene el token CSRF y lo envía siempre."""
    token = raw_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    raw_client.headers["X-CSRF-Token"] = token
    return raw_client
