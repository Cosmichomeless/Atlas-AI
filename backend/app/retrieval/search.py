"""Búsqueda semántica: los k fragmentos más parecidos a una pregunta, con puntuación y origen."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.store import SimilarChunk, nearest_chunks
from app.features.documents.states import DocumentStatus
from app.retrieval.dedup import DedupPolicy, Reduction, reduce_redundancy
from app.retrieval.question import PreparedQuestion
from app.retrieval.rerank import Reranking, RerankPolicy, rerank


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
    # None = sin reducir redundancia (los resultados son tal cual los devuelve la consulta).
    dedup: DedupPolicy | None = None
    # None = el orden es el de la similitud vectorial (comportamiento por defecto).
    rerank: RerankPolicy | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "SearchLimits":
        return cls(
            settings.search_default_k,
            settings.search_max_k,
            settings.search_min_score,
            settings.search_max_documents,
            DedupPolicy(
                settings.search_dedup_overlap,
                settings.search_dedup_window,
                settings.search_overfetch,
            ),
            RerankPolicy(
                settings.search_rerank,
                settings.search_rerank_weight,
                settings.search_rerank_pool,
            )
            if settings.search_rerank != "off"
            else None,
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


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Resultado final; con dedup, qué se descartó, y con reranking, cómo se reordenó."""

    hits: list[SimilarChunk]
    reduction: Reduction | None
    reranking: Reranking | None = None


def search_with_report(
    session: Session,
    question: PreparedQuestion,
    *,
    owner_id: uuid.UUID,
    limits: SearchLimits,
    k: int | None = None,
    min_score: float | None = None,
    document_ids: Collection[uuid.UUID] | None = None,
    status: DocumentStatus | None = None,
) -> SearchOutcome:
    """Hasta `k` fragmentos del usuario con similitud >= `min_score`, de más a menos parecido.

    Solo busca entre los documentos de `owner_id`; con `document_ids` además se limita a esa
    selección. Un identificador ajeno o inexistente no da error ni se distingue del otro: no aporta
    resultados. Una selección vacía no busca en ningún documento (nunca equivale a "todos").
    `status` limita la búsqueda a documentos en ese estado (la API pasa READY).

    Con `limits.dedup` se piden `overfetch` veces más candidatos, se descartan los repetidos y
    contiguos (ver `app.retrieval.dedup`) y se recorta a `k`: lo redundante no ocupa plazas que
    corresponden a otras fuentes. Con `limits.rerank` se piden además `pool` veces más, y tras la
    reducción se reordenan con la puntuación combinada (ver `app.retrieval.rerank`) antes de
    recortar: cambia el orden, no el contrato ni la puntuación vectorial de cada resultado.

    Los empates se resuelven por documento y orden dentro de él, así que el resultado es
    determinista. Si nada alcanza el umbral devuelve una lista vacía.
    """
    k, min_score = limits.resolve(k, min_score)
    scope = limits.resolve_scope(document_ids)
    policy, reranker = limits.dedup, limits.rerank
    pool = max(policy.overfetch if policy else 1, reranker.pool if reranker else 1)
    candidates = nearest_chunks(
        session,
        question.embedding,
        owner_id=owner_id,
        limit=k * pool,
        max_distance=1.0 - min_score,
        document_ids=scope,
        status=status,
    )
    reduction = reduce_redundancy(candidates, policy) if policy else None
    kept = reduction.kept if reduction else candidates
    reranking = rerank(question.text, kept, reranker) if reranker else None
    ordered = reranking.ordered if reranking else kept
    return SearchOutcome(ordered[:k], reduction, reranking)


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
    """Como `search_with_report`, pero solo los resultados."""
    return search_with_report(
        session,
        question,
        owner_id=owner_id,
        limits=limits,
        k=k,
        min_score=min_score,
        document_ids=document_ids,
        status=status,
    ).hits
