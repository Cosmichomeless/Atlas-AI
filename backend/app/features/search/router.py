import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.errors import AppError, error_responses
from app.core.config import get_settings
from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.embeddings.store import SimilarChunk
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.documents.states import DocumentStatus
from app.retrieval.question import (
    IncompatibleIndexError,
    InvalidQuestionError,
    ensure_index_compatible,
    prepare_question,
)
from app.retrieval.search import InvalidSearchError, SearchLimits, search_chunks

router = APIRouter(prefix="/search", tags=["search"], responses=error_responses(401))

SNIPPET_MAX_CHARS = 280
MAX_SELECTED_IDS = 1000  # tope duro del cuerpo; el límite funcional es SEARCH_MAX_DOCUMENTS


def get_search_limits() -> SearchLimits:
    """Límites de búsqueda (`SEARCH_*`); dependencia sustituible en las pruebas."""
    return SearchLimits.from_settings(get_settings())


class SearchRequest(BaseModel):
    question: str = Field(max_length=10_000, description="Pregunta en lenguaje natural.")
    k: int | None = Field(default=None, description="Máximo de resultados (por defecto SEARCH_*).")
    min_score: float | None = Field(
        default=None, description="Similitud coseno mínima, de 0 a 1 (por defecto SEARCH_*)."
    )
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=MAX_SELECTED_IDS,
        description=(
            "Limita la búsqueda a estos documentos. Omitido = todos los míos listos; "
            "ajenos o inexistentes no aportan resultados."
        ),
    )


class SearchSource(BaseModel):
    """Dónde está el fragmento: lo necesario para citarlo y abrir el original."""

    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    ordinal: int = Field(description="Posición del fragmento dentro del documento.")
    page: int | None
    section: str | None
    start_line: int | None
    end_line: int | None


class SearchResult(BaseModel):
    snippet: str = Field(description="Texto breve del fragmento, acortado si es largo.")
    score: float = Field(description="Similitud coseno en [-1, 1]; 1 = misma dirección.")
    source: SearchSource


class SearchResponse(BaseModel):
    results: list[SearchResult]
    k: int = Field(description="Máximo de resultados aplicado.")
    min_score: float = Field(description="Umbral de similitud aplicado.")


def make_snippet(text: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """Texto en una sola línea y acortado en un límite de palabra, con «…» si se recorta."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_chars:
        return flat
    cut = flat[: max_chars - 1]
    if " " in cut and flat[max_chars - 1] != " ":
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def to_result(hit: SimilarChunk) -> SearchResult:
    chunk, document = hit.chunk, hit.document
    return SearchResult(
        snippet=make_snippet(chunk.text),
        score=hit.score,
        source=SearchSource(
            document_id=document.id,
            filename=document.filename,
            chunk_id=chunk.id,
            ordinal=chunk.ordinal,
            page=chunk.page,
            section=chunk.section,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
        ),
    )


@router.post(
    "",
    summary="Buscar fragmentos relevantes",
    responses=error_responses(409, 422, 503),
)
def search(
    body: SearchRequest,
    user: CurrentUser,
    session: SessionDep,
    embedder: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
    limits: Annotated[SearchLimits, Depends(get_search_limits)],
) -> SearchResponse:
    """Fragmentos de mis documentos listos (READY) más parecidos a la pregunta, con su origen."""
    k, min_score = _resolve(limits, body)
    try:
        question = prepare_question(
            body.question, embedder, max_chars=get_settings().question_max_chars
        )
        ensure_index_compatible(session, question.embedding.spec, owner_id=user.id)
        hits = search_chunks(
            session,
            question,
            owner_id=user.id,
            limits=limits,
            k=k,
            min_score=min_score,
            document_ids=body.document_ids,
            status=DocumentStatus.READY,
        )
    except (InvalidQuestionError, InvalidSearchError) as error:
        raise AppError(422, error.code, error.message) from error
    except IncompatibleIndexError as error:
        raise AppError(
            409,
            "index_incompatible",
            "Tus documentos se indexaron con otro modelo de embeddings: reindéxalos para buscar.",
        ) from error
    except EmbeddingError as error:
        raise AppError(
            503, "embedding_unavailable", "No se pudo procesar la pregunta. Inténtalo más tarde."
        ) from error
    return SearchResponse(results=[to_result(hit) for hit in hits], k=k, min_score=min_score)


def _resolve(limits: SearchLimits, body: SearchRequest) -> tuple[int, float]:
    try:
        return limits.resolve(body.k, body.min_score)
    except InvalidSearchError as error:
        raise AppError(422, error.code, error.message) from error
