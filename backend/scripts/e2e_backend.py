"""Backend para las pruebas E2E: base de datos limpia, migraciones, worker y API en un solo comando.

Lo lanza Playwright (`frontend/playwright.config.ts`) y también se puede ejecutar a mano:

    E2E_DATABASE_URL=postgresql+psycopg://atlas:…@localhost:5433/atlas_e2e \\
        uv run python scripts/e2e_backend.py

Cada arranque borra y recrea la base de datos indicada, por eso se niega a tocar una cuya nombre
no termine en `_e2e`. Los proveedores son los `fake` (sin red ni credenciales) y el LLM fake
responde con lo que dicen las fuentes y las cita. Al recibir SIGTERM/SIGINT para el worker y la API.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parent.parent
API_PORT = os.environ.get("E2E_API_PORT", "8765")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://127.0.0.1:3765")


def reset_database(url: str) -> None:
    """Borra y recrea la base de datos, con pgvector habilitado."""
    parsed = make_url(url)
    name = parsed.database or ""
    if not name.endswith("_e2e"):
        sys.exit(
            f"E2E_DATABASE_URL debe apuntar a una base cuyo nombre termine en «_e2e» (es «{name}»)."
        )
    admin = parsed.set(drivername="postgresql", database="postgres")
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as conn:
        ident = sql.Identifier(name)
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(ident))
    plain = parsed.set(drivername="postgresql")
    with psycopg.connect(plain.render_as_string(hide_password=False), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def main() -> int:
    url = os.environ.get("E2E_DATABASE_URL")
    if not url:
        sys.exit(
            "Falta E2E_DATABASE_URL (p. ej. postgresql+psycopg://atlas:…@localhost:5433/atlas_e2e)."
        )
    reset_database(url)

    storage = tempfile.mkdtemp(prefix="atlas-e2e-")
    env = {
        **os.environ,
        "APP_ENV": "development",
        "DATABASE_URL": url,
        "STORAGE_DIR": storage,
        "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
        "EMBEDDING_PROVIDER": "fake",
        "LLM_PROVIDER": "fake",
        "FAKE_LLM_GROUNDED": "true",
        "INGESTION_POLL_SECONDS": "1",
        "PYTHONUNBUFFERED": "1",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, check=True
    )

    processes = [
        subprocess.Popen([sys.executable, "-m", "app.ingestion.worker"], cwd=BACKEND, env=env),
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                API_PORT,
            ],
            cwd=BACKEND,
            env=env,
        ),
    ]

    def stop(*_: object) -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Si cualquiera de los dos muere, no tiene sentido seguir: la prueba fallaría confusa.
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(storage, ignore_errors=True)
    return max(abs(process.returncode or 0) for process in processes)


if __name__ == "__main__":
    sys.exit(main())
