import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

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
