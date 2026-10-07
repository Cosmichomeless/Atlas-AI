"""Proveedor falso: determinista, sin red y sin coste, para desarrollo y tests."""

import hashlib
import math
import re
from collections.abc import Sequence

from app.embeddings.provider import EmbeddingProvider, EmbeddingSpec

FAKE_VERSION = "1"
_WORD = re.compile(r"\w+")


class FakeEmbeddingProvider(EmbeddingProvider):
    """Vectores por hashing de palabras (bolsa de palabras) normalizados a longitud 1.

    El mismo texto da siempre el mismo vector y textos que comparten palabras quedan más cerca,
    lo bastante para probar la recuperación sin llamar a ningún servicio de pago.
    """

    def __init__(self, model: str, dimensions: int) -> None:
        self.spec = EmbeddingSpec("fake", model, dimensions, FAKE_VERSION)

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.spec.dimensions
        for token in _WORD.findall(text.lower()) or [text]:
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.spec.dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:  # palabras que se anulan entre sí: cae al hash del texto completo
            digest = hashlib.sha256(text.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % len(vector)] = 1.0
            return vector
        return [value / norm for value in vector]
