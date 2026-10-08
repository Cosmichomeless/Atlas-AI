"""Reintentos acotados con espera exponencial para fallos transitorios de proveedores externos.

Solo se reintenta lo que merece la pena: caídas de red, plazos agotados, 429 y 5xx. Un 401 o un 400
no mejoran por esperar, así que fallan a la primera. El número de intentos es un tope duro y la
espera crece (`base`, `2·base`, …) hasta `max_delay`, con un tope también para el `Retry-After` que
anuncie el servicio, de modo que una petición nunca queda colgada indefinidamente.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx2

logger = logging.getLogger(__name__)

# Códigos HTTP con sentido de "inténtalo de nuevo": plazo, conflicto de bloqueo, demasiado pronto,
# límite de tasa y caídas del servidor.
TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """`attempts` es el total de llamadas (1 = sin reintentos)."""

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay(self, retry: int, retry_after: float | None = None) -> float:
        """Espera antes del reintento número `retry` (1 = el primero)."""
        wait: float = self.base_delay * 2 ** (retry - 1)
        if retry_after is not None:
            wait = max(wait, retry_after)
        return float(min(wait, self.max_delay))


NO_RETRY = RetryPolicy(attempts=1, base_delay=0.0, max_delay=0.0)


def is_transient(error: httpx2.HTTPError) -> bool:
    """¿Merece reintento? Fallos de transporte y estados transitorios, no errores del cliente."""
    if isinstance(error, httpx2.HTTPStatusError):
        return error.response.status_code in TRANSIENT_STATUS
    return isinstance(error, httpx2.TransportError)


def retry_after_seconds(error: httpx2.HTTPError) -> float | None:
    """Segundos que pide esperar el servicio (`Retry-After` numérico), si los indica."""
    if not isinstance(error, httpx2.HTTPStatusError):
        return None
    try:
        seconds = float(error.response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def call_with_retries[T](
    call: Callable[[], T],
    policy: RetryPolicy,
    *,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Ejecuta `call` reintentando los `httpx2.HTTPError` transitorios; relanza el último fallo."""
    attempt = 1
    while True:
        try:
            return call()
        except httpx2.HTTPError as error:
            if attempt >= policy.attempts or not is_transient(error):
                raise
            wait = policy.delay(attempt, retry_after_seconds(error))
            logger.warning(
                "%s: fallo transitorio (%s); reintento %d/%d en %.1f s",
                what,
                _describe(error),
                attempt,
                policy.attempts - 1,
                wait,
            )
            sleep(wait)
            attempt += 1


def _describe(error: httpx2.HTTPError) -> str:
    if isinstance(error, httpx2.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__
