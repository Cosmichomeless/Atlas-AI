"""Estimación de tokens sin depender de un tokenizador concreto."""

import math

CHARS_PER_TOKEN = 3
"""Caracteres por token. Es una cota prudente (el español ronda 3,5-4; cifras y código, menos):
sobrestimar solo cuesta algo de contexto, subestimar rompería el límite del modelo."""


def estimate_tokens(text: str) -> int:
    """Cota superior aproximada de los tokens de `text`: nunca menos que sus palabras."""
    if not text:
        return 0
    return max(math.ceil(len(text) / CHARS_PER_TOKEN), len(text.split()))


FRAGMENT_OVERHEAD_TOKENS = 100
"""Reserva por fragmento para su cabecera (etiqueta, documento, página, sección) y el separador."""


def min_context_tokens(chunk_size_chars: int) -> int:
    """Presupuesto mínimo con sentido: que quepa al menos un fragmento del tamaño máximo."""
    return estimate_tokens("x" * chunk_size_chars) + FRAGMENT_OVERHEAD_TOKENS
