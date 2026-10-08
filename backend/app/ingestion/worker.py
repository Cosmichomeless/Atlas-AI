"""Proceso de ingestión: `python -m app.ingestion.worker` (desde `backend/`).

Corre fuera de la API, así que subir un archivo no espera a la extracción. Se puede lanzar más de un
proceso a la vez; se detiene limpiamente con SIGINT/SIGTERM tras terminar el documento en curso.
"""

import logging
import signal
import threading
from types import FrameType

from sqlalchemy import Engine

import app.models  # noqa: F401  (registra todos los modelos: ver app/models.py)
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.schema import expected_revision, is_current
from app.embeddings.registry import get_embedding_provider
from app.features.documents.extraction import ExtractionLimits
from app.features.documents.storage import get_storage
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.heartbeat import beat
from app.ingestion.service import run_once

logger = logging.getLogger("atlas.ingestion")

MAX_BACKOFF_SECONDS = 30.0


def run(stop: threading.Event) -> None:
    settings = get_settings()
    storage = get_storage()
    policy = ChunkPolicy.from_settings(settings)
    limits = ExtractionLimits.from_settings(settings)
    embedder = get_embedding_provider()
    sessionmaker = get_sessionmaker()
    wait_for_schema(stop, settings.ingestion_poll_seconds, sessionmaker.kw["bind"])
    logger.info("Worker de ingestión iniciado")
    failures = 0
    while not stop.is_set():
        try:
            # Sesión nueva por documento: un fallo no contamina el siguiente
            with sessionmaker() as session:
                worked = run_once(
                    session,
                    storage,
                    policy=policy,
                    embedder=embedder,
                    lease_seconds=settings.ingestion_lease_seconds,
                    max_attempts=settings.ingestion_max_attempts,
                    limits=limits,
                )
            failures = 0
            beat(settings.ingestion_heartbeat_file)
        except Exception:
            failures += 1
            logger.exception(
                "Fallo del worker (¿base de datos no disponible?); se reintenta (%d seguidos)",
                failures,
            )
            worked = False
        if not worked:
            stop.wait(idle_wait(settings.ingestion_poll_seconds, failures))
    logger.info("Worker de ingestión detenido")


def wait_for_schema(stop: threading.Event, poll_seconds: float, engine: Engine) -> None:
    """No procesa nada hasta que la base esté migrada a la revisión de este código."""
    announced = False
    while not stop.is_set() and not is_current(engine):
        if not announced:
            logger.warning(
                "La base de datos no está en la revisión %s; el worker espera a las migraciones",
                expected_revision(),
            )
            announced = True
        stop.wait(max(poll_seconds, 1.0))


def idle_wait(poll_seconds: float, failures: int) -> float:
    """Espera entre vueltas: la de sondeo, o una creciente (con tope) si no se recupera."""
    if failures == 0:
        return poll_seconds
    return float(min(poll_seconds * 2 ** min(failures, 6), MAX_BACKOFF_SECONDS))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("Señal %s recibida; terminando tras el documento en curso", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run(stop)


if __name__ == "__main__":
    main()
