"""Los scripts backup.sh y restore.sh hacen un viaje de ida y vuelta sin perder datos.

Se ejecutan los scripts reales (modo `host`) contra una base de datos temporal del mismo servidor
que usan los tests. Se omiten si faltan las herramientas de cliente de PostgreSQL. El modo `compose`
usa los mismos comandos a través de `docker compose` y no se prueba aquí.
"""

import io
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage, document_key
from app.features.users.models import User
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL

SCRIPTS = BACKEND_DIR.parent / "scripts"
TOOLS = ("pg_dump", "pg_restore", "psql", "tar", "sha256sum", "bash")

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in TOOLS),
    reason="faltan pg_dump/pg_restore/psql, tar, sha256sum o bash",
)


def _admin_url(database: str = "postgres") -> str:
    return (
        make_url(TEST_DATABASE_URL)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


def _recreate(name: str) -> None:
    with psycopg.connect(_admin_url(), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(_admin_url(name), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _client_is_new_enough() -> bool:
    pg_dump = shutil.which("pg_dump") or "pg_dump"
    out = subprocess.run([pg_dump, "--version"], capture_output=True, text=True, check=True).stdout  # noqa: S603
    match = re.search(r"\(PostgreSQL\) (\d+)", out)
    assert match, out
    with psycopg.connect(_admin_url()) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    return int(match.group(1)) >= int(str(row[0])[:-4])


@pytest.fixture
def database() -> Iterator[str]:
    if not _client_is_new_enough():
        pytest.skip("pg_dump es más antiguo que el servidor")
    name = f"atlas_bk_{uuid.uuid4().hex[:8]}"
    _recreate(name)
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    try:
        yield url
    finally:
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _env(url: str, storage: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ATLAS_MODE": "host",
        "DATABASE_URL": url,
        "ATLAS_STORAGE_DIR": str(storage),
        "ATLAS_PYTHON": sys.executable,
    }


def _run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(SCRIPTS / script), *args], env=env, capture_output=True, text=True, check=False
    )


def _seed(url: str, storage_dir: Path) -> tuple[uuid.UUID, str]:
    engine = create_engine(url)
    storage = LocalFileStorage(storage_dir)
    with Session(engine) as session:
        user = User(email="ana@example.com", password_hash="x")
        session.add(user)
        session.flush()
        document_id = uuid.uuid4()
        key = document_key(user.id, document_id)
        document = Document(
            id=document_id,
            owner_id=user.id,
            filename="contrato.txt",
            content_type="text/plain",
            size_bytes=11,
            storage_key=key,
            status=DocumentStatus.UPLOADED,
        )
        session.add(document)
        session.flush()
        document.status = DocumentStatus.PROCESSING
        document.status = DocumentStatus.READY
        session.commit()
    storage.save(key, io.BytesIO(b"hola mundo!!"))
    engine.dispose()
    return document_id, key


def _count(url: str, table: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
    finally:
        engine.dispose()


def test_a_backup_can_be_restored_after_losing_everything(database: str, tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    out = tmp_path / "backups"
    _, key = _seed(database, storage)
    env = _env(database, storage)

    made = _run("backup.sh", "--out", str(out), env=env)
    assert made.returncode == 0, made.stderr
    (backup,) = list(out.iterdir())
    manifest = (backup / "MANIFEST.txt").read_text()
    assert "users=1" in manifest and "documents=1" in manifest and "storage_files=1" in manifest
    assert re.search(r"alembic_revision=\w+", manifest)
    assert oct(backup.stat().st_mode & 0o777) == "0o700"

    # Desastre: base de datos vacía y sin archivos.
    name = make_url(database).database
    assert name is not None
    _recreate(name)
    shutil.rmtree(storage)

    # Sin --yes no toca nada.
    refused = _run("restore.sh", str(backup), env=env)
    assert refused.returncode == 2
    assert "--yes" in refused.stdout

    restored = _run("restore.sh", str(backup), "--yes", env=env)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert "Todo cuadra" in restored.stdout
    assert _count(database, "users") == 1
    assert _count(database, "documents") == 1
    assert (storage / key).read_bytes() == b"hola mundo!!"


def test_a_corrupted_backup_is_refused_before_touching_anything(
    database: str, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    out = tmp_path / "backups"
    _seed(database, storage)
    env = _env(database, storage)
    assert _run("backup.sh", "--out", str(out), env=env).returncode == 0
    (backup,) = list(out.iterdir())
    with (backup / "db.dump").open("ab") as dump:
        dump.write(b"corrupcion")

    result = _run("restore.sh", str(backup), "--yes", env=env)

    assert result.returncode != 0
    assert "dañada" in result.stderr
    assert _count(database, "documents") == 1  # la base original sigue intacta


def test_old_backups_are_pruned_but_the_newest_are_kept(database: str, tmp_path: Path) -> None:
    out = tmp_path / "backups"
    for day in ("20200101", "20200102", "20200103"):
        (out / f"atlas-{day}T000000Z").mkdir(parents=True)
    env = _env(database, tmp_path / "storage")

    result = _run("backup.sh", "--out", str(out), "--keep", "2", env=env)

    assert result.returncode == 0, result.stderr
    kept = sorted(p.name for p in out.iterdir())
    assert len(kept) == 2
    assert kept[0] == "atlas-20200103T000000Z"
    assert kept[1] > "atlas-2026"


def test_restore_rejects_a_folder_that_is_not_a_backup(database: str, tmp_path: Path) -> None:
    result = _run("restore.sh", str(tmp_path), "--yes", env=_env(database, tmp_path / "storage"))

    assert result.returncode != 0
    assert "no parece una copia" in result.stderr
