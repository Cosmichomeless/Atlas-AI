import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def empty_database() -> Iterator[str]:
    """Crea una base vacía (sin extensiones) y la elimina al terminar."""
    admin_url = make_url(TEST_DATABASE_URL).set(database="postgres")
    name = f"atlas_migr_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["url"] = url
    return cfg


def test_upgrade_creates_schema_from_empty_database(empty_database: str) -> None:
    command.upgrade(_alembic(empty_database), "head")

    engine = create_engine(empty_database)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar()
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001"
    engine.dispose()


def test_upgrade_is_idempotent_and_keeps_existing_data(empty_database: str) -> None:
    cfg = _alembic(empty_database)
    command.upgrade(cfg, "head")

    engine = create_engine(empty_database)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sentinel (id int PRIMARY KEY, label text)"))
        conn.execute(text("INSERT INTO sentinel VALUES (1, 'keep me')"))

    command.upgrade(cfg, "head")  # repetir no debe hacer nada ni fallar

    with engine.connect() as conn:
        assert conn.execute(text("SELECT label FROM sentinel WHERE id = 1")).scalar() == "keep me"
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 1
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001"
    engine.dispose()


def test_downgrade_and_upgrade_roundtrip(empty_database: str) -> None:
    cfg = _alembic(empty_database)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(empty_database)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar()
    engine.dispose()
