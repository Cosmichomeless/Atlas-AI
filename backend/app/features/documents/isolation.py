"""Ejecución del análisis en un subproceso con plazo: un parser colgado se mata, no se espera.

`pypdf` es Python puro y no se puede interrumpir desde un hilo; un PDF malicioso o corrupto podría
dejar al worker bloqueado para siempre. Aquí el archivo viaja por la entrada estándar a un
intérprete nuevo (`-I`: sin variables de entorno de Python ni directorio actual en el `sys.path`)
que ejecuta `parsing.py` como script, con el entorno vacío —sin credenciales— y con tope de CPU.
Si el hijo supera el plazo, `subprocess.run` lo mata y espera su fin: no queda proceso colgado. Si
el padre desaparece, el vigilante de reloj del hijo (`signal.alarm`) lo termina igualmente.
"""

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.features.documents import parsing
from app.features.documents.parsing import Limits, ParsedBlock, ParseError

logger = logging.getLogger(__name__)

_SCRIPT = Path(parsing.__file__).resolve()
# Cota de la respuesta del hijo (JSON): el texto más cierta sobrecarga por bloque y escapes.
_OUTPUT_OVERHEAD_BYTES = 1024 * 1024
_STDERR_LOGGED_BYTES = 2000


@dataclass(frozen=True)
class Isolation:
    timeout_seconds: int


def run_isolated(
    content_type: str, data: bytes, limits: Limits, isolation: Isolation
) -> list[ParsedBlock]:
    """Analiza `data` en un subproceso; todo fallo se convierte en un `ParseError` comprensible."""
    command = [
        sys.executable,
        "-I",
        str(_SCRIPT),
        content_type,
        str(limits.max_pages),
        str(limits.max_chars),
        str(isolation.timeout_seconds),
    ]
    try:
        done = subprocess.run(  # noqa: S603 - intérprete propio y argumentos que no vienen del cliente
            command,
            input=data,
            capture_output=True,
            timeout=isolation.timeout_seconds,
            env={},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseError(
            f"El documento tardó más de {isolation.timeout_seconds} s en procesarse y se canceló."
        ) from exc
    except OSError as exc:
        logger.exception("No se pudo lanzar el subproceso de extracción")
        raise ParseError("No se pudo procesar el documento: el análisis no arrancó.") from exc

    if done.returncode != 0:
        logger.warning(
            "El subproceso de extracción terminó con código %s: %s",
            done.returncode,
            done.stderr[-_STDERR_LOGGED_BYTES:].decode("utf-8", "replace"),
        )
        raise ParseError(
            "No se pudo procesar el documento: el análisis terminó de forma inesperada "
            "(puede exceder los recursos permitidos)."
        )
    if len(done.stdout) > limits.max_chars * 8 + _OUTPUT_OVERHEAD_BYTES:
        raise ParseError("El análisis del documento devolvió más datos de los permitidos.")
    return _decode(done.stdout)


def _decode(output: bytes) -> list[ParsedBlock]:
    try:
        result = json.loads(output)
        if "error" in result:
            raise ParseError(str(result["error"]))
        return [ParsedBlock(**block) for block in result["blocks"]]
    except ParseError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Respuesta ilegible del subproceso de extracción: %s", exc)
        raise ParseError(
            "No se pudo procesar el documento: respuesta del análisis inválida."
        ) from exc
