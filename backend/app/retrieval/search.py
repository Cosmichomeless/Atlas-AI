"""Búsqueda semántica: los k fragmentos más parecidos a una pregunta, con puntuación y origen."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.store import SimilarChunk, nearest_chunks
from app.features.documents.states import DocumentStatus
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
    max_documents: int = 50

    @classmethod
    def from_settings(cls, settings: Settings) -> "SearchLimits":
        return cls(
            settings.search_default_k,
            settings.search_max_k,
            settings.search_min_score,
            settings.search_max_documents,
        )

    def resolve_scope(self, document_ids: Collection[uuid.UUID] | None) -> set[uuid.UUID] | None:
        """Documentos seleccionados, sin repetidos y dentro del máximo; `None` = todos los míos."""
        if document_ids is None:
            return None
        scope = set(document_ids)
        if len(scope) > self.max_documents:
            raise InvalidSearchError(
                "too_many_documents",
                f"Puedes limitar la búsqueda a {self.max_documents} documentos como máximo.",
            )
        return scope

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
    document_ids: Collection[uuid.UUID] | None = None,
    status: DocumentStatus | None = None,
) -> list[SimilarChunk]:
    """Hasta `k` fragmentos del usuario con similitud >= `min_score`, de más a menos parecido.

    Solo busca entre los documentos de `owner_id`; con `document_ids` además se limita a esa
    selección. Un identificador ajeno o inexistente no da error ni se distingue del otro: no aporta
    resultados. Una selección vacía no busca en ningún documento (nunca equivale a "todos").
    `status` limita la búsqueda a documentos en ese estado (la API pasa READY).

    Los empates se resuelven por documento y orden dentro de él, así que el resultado es
    determinista. Si nada alcanza el umbral devuelve una lista vacía.
    """
    k, min_score = limits.resolve(k, min_score)
    scope = limits.resolve_scope(document_ids)
    return nearest_chunks(
        session,
        question.embedding,
        owner_id=owner_id,
        limit=k,
        max_distance=1.0 - min_score,
        document_ids=scope,
        status=status,
    )
