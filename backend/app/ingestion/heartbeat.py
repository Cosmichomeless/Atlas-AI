"""Latido del worker: un archivo que se toca tras cada vuelta sana.

Es lo que vigila el health check del contenedor (`python -m app.ingestion.heartbeat`): el worker no
escucha en ningún puerto. Si la base de datos cae o el proceso se cuelga, el latido envejece y el
contenedor pasa a `unhealthy`.
"""

import sys
import time
from pathlib import Path

from app.core.config import get_settings


def beat(path: Path | None) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def is_alive(path: Path | None, max_age_seconds: float, now: float | None = None) -> bool:
    if path is None:
        return False
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age <= max_age_seconds


def main() -> int:
    settings = get_settings()
    # Una vuelta puede durar lo que una extracción; pasado el arriendo, el worker está muerto.
    alive = is_alive(settings.ingestion_heartbeat_file, settings.ingestion_lease_seconds)
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
