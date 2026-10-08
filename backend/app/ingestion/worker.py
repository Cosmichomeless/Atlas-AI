"""Proceso de ingestión: `python -m app.ingestion.worker` (desde `backend/`).

Corre fuera de la API, así que subir un archivo no espera a la extracción. Se puede lanzar más de un
proceso a la vez; se detiene limpiamente con SIGINT/SIGTERM tras terminar el documento en curso.
"""

import logging
import signal
import threading
from types import FrameType

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.embeddings.registry import get_embedding_provider
from app.features.documents.extraction import ExtractionLimits
from app.features.documents.storage import get_storage
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.service import run_once

logger = logging.getLogger("atlas.ingestion")


def run(stop: threading.Event) -> None:
    settings = get_settings()
    storage = get_storage()
    policy = ChunkPolicy.from_settings(settings)
    limits = ExtractionLimits.from_settings(settings)
    embedder = get_embedding_provider()
    sessionmaker = get_sessionmaker()
    logger.info("Worker de ingestión iniciado")
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
        except Exception:
            logger.exception("Fallo del worker (¿base de datos no disponible?); se reintenta")
            worked = False
        if not worked:
            stop.wait(settings.ingestion_poll_seconds)
    logger.info("Worker de ingestión detenido")


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
