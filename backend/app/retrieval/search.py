"""Búsqueda semántica: los k fragmentos más parecidos a una pregunta, con puntuación y origen."""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.store import SimilarChunk, nearest_chunks
from app.retrieval.question import PreparedQuestion


class InvalidSearchError(ValueError):
    """Parámetros de búsqueda fuera de los límites configurados; el mensaje es apto para mostrar."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Valores por defecto y tope de `k`, y umbral mínimo de similitud (`SEARCH_*`)."""

    default_k: int = 5
    max_k: int = 20
    min_score: float = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "SearchLimits":
        return cls(settings.search_default_k, settings.search_max_k, settings.search_min_score)

    def resolve(self, k: int | None, min_score: float | None) -> tuple[int, float]:
        """`k` y umbral efectivos, validados; lo no indicado toma el valor por defecto."""
        k = self.default_k if k is None else k
        min_score = self.min_score if min_score is None else min_score
        if not 1 <= k <= self.max_k:
            raise InvalidSearchError("invalid_k", f"k debe estar entre 1 y {self.max_k}.")
        if not 0.0 <= min_score <= 1.0:
            raise InvalidSearchError(
                "invalid_min_score", "El umbral de similitud debe estar entre 0 y 1."
            )
        return k, min_score


def search_chunks(
    session: Session,
    question: PreparedQuestion,
    *,
    owner_id: uuid.UUID,
    limits: SearchLimits,
    k: int | None = None,
    min_score: float | None = None,
) -> list[SimilarChunk]:
    """Hasta `k` fragmentos del usuario con similitud >= `min_score`, de más a menos parecido.

    Los empates se resuelven por documento y orden dentro de él, así que el resultado es
    determinista. Si nada alcanza el umbral devuelve una lista vacía.
    """
    k, min_score = limits.resolve(k, min_score)
    return nearest_chunks(
        session,
        question.embedding,
        owner_id=owner_id,
        limit=k,
        max_distance=1.0 - min_score,
    )
