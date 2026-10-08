"""Piezas que hacen operable el worker en un contenedor: latido, esquema y arranque limpio."""

import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.core.schema import applied_revision, expected_revision, is_current
from app.ingestion import worker
from app.ingestion.heartbeat import beat, is_alive


def test_a_fresh_heartbeat_is_alive_and_an_old_or_missing_one_is_not(tmp_path: Path) -> None:
    path = tmp_path / "state" / "heartbeat"
    assert not is_alive(path, 60)
    assert not is_alive(None, 60)

    beat(path)

    assert is_alive(path, 60)
    assert not is_alive(path, 60, now=time.time() + 61)


def test_beating_without_a_configured_file_does_nothing() -> None:
    beat(None)


def test_an_empty_heartbeat_setting_means_disabled() -> None:
    settings = Settings(database_url="postgresql+psycopg://u:p@h/db", ingestion_heartbeat_file="")  # type: ignore[arg-type]
    assert settings.ingestion_heartbeat_file is None


def test_the_database_is_on_the_revision_this_code_ships(db_engine: Engine) -> None:
    assert expected_revision() is not None
    assert applied_revision(db_engine) == expected_revision()
    assert is_current(db_engine)


def test_a_database_on_another_revision_is_not_current(db_engine: Engine) -> None:
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'old'"))
    try:
        assert not is_current(db_engine)
    finally:
        with db_engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :v"), {"v": expected_revision()}
            )


def test_the_worker_waits_for_migrations_instead_of_processing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "is_current", lambda engine: False)
    stop = threading.Event()
    finished = threading.Thread(
        target=worker.wait_for_schema, args=(stop, 0.01, db_engine), daemon=True
    )
    finished.start()
    finished.join(timeout=0.3)
    assert finished.is_alive()

    stop.set()
    finished.join(timeout=3)
    assert not finished.is_alive()
