import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.answers.citations import Citation
from app.answers.context import BoundedContext
from app.answers.generate import Answer
from app.answers.service import Abstained, AbstentionReason, Answered, answer_question
from app.answers.tokens import estimate_tokens
from app.api.errors import AppError, error_responses
from app.core.config import get_settings
from app.embeddings.provider import EmbeddingError, EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.documents.states import DocumentStatus
from app.features.search.router import MAX_SELECTED_IDS, get_search_limits
from app.features.usage.service import (
    Tally,
    UsageLimitExceeded,
    UsageLimits,
    ensure_within_limits,
    record_usage,
)
from app.llm.provider import LLMError, LLMProvider
from app.llm.registry import get_llm_provider
from app.retrieval.question import (
    IncompatibleIndexError,
    InvalidQuestionError,
    ensure_index_compatible,
    prepare_question,
)
from app.retrieval.search import InvalidSearchError, SearchLimits, search_chunks

router = APIRouter(prefix="/questions", tags=["questions"], responses=error_responses(401))


class QuestionRequest(BaseModel):
    question: str = Field(max_length=10_000, description="Pregunta en lenguaje natural.")
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=MAX_SELECTED_IDS,
        description=(
            "Limita la respuesta a estos documentos. Omitido = todos los míos listos (READY); "
            "ajenos, inexistentes o no listos no aportan fuentes."
        ),
    )


class QuestionCitation(BaseModel):
    """Fuente verificada de la respuesta: documento y ubicación exacta del fragmento."""

    label: str = Field(description="Etiqueta que aparece en el texto, p. ej. «S1» en «[S1]».")
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    ordinal: int = Field(description="Posición del fragmento dentro del documento.")
    page: int | None
    section: str | None
    start_line: int | None
    end_line: int | None


class ConsultedDocument(BaseModel):
    document_id: uuid.UUID
    filename: str


class AnswerProvenance(BaseModel):
    """Con qué prompt y modelo se generó la respuesta."""

    prompt_version: str
    prompt_fingerprint: str
    llm: str = Field(description="Proveedor, modelo y versión, p. ej. «openai/gpt-x/v1».")
    temperature: float
    max_output_tokens: int


class QuestionResponse(BaseModel):
    status: Literal["answered", "abstained"] = Field(
        description="«abstained»: no hay evidencia suficiente y no se entrega respuesta."
    )
    text: str | None = Field(description="Respuesta con etiquetas de cita; nula si se abstiene.")
    abstention_reason: AbstentionReason | None = Field(
        description="Por qué no se responde; nulo si hay respuesta."
    )
    citations: list[QuestionCitation] = Field(description="Solo citas verificadas.")
    uncited_statements: list[str] = Field(
        description="Frases de la respuesta sin fuente ni marca de ajenas a los documentos."
    )
    documents: list[ConsultedDocument] = Field(
        description="Documentos de los que salieron los fragmentos consultados."
    )
    truncated: bool = Field(description="La respuesta se cortó por el límite de salida del modelo.")
    provenance: AnswerProvenance | None = Field(
        description="Nulo si no se llegó a consultar al modelo."
    )


def _citation(citation: Citation) -> QuestionCitation:
    return QuestionCitation(
        label=citation.label,
        document_id=citation.document_id,
        filename=citation.filename,
        chunk_id=citation.chunk_id,
        ordinal=citation.ordinal,
        page=citation.page,
        section=citation.section,
        start_line=citation.start_line,
        end_line=citation.end_line,
    )


def _documents(context: BoundedContext) -> list[ConsultedDocument]:
    seen: dict[uuid.UUID, ConsultedDocument] = {}
    for item in context.items:
        seen.setdefault(
            item.document_id,
            ConsultedDocument(document_id=item.document_id, filename=item.filename),
        )
    return list(seen.values())


def _provenance(answer: Answer | None) -> AnswerProvenance | None:
    if answer is None:
        return None
    p = answer.provenance
    return AnswerProvenance(
        prompt_version=p.prompt_version,
        prompt_fingerprint=p.prompt_fingerprint,
        llm=p.llm,
        temperature=p.temperature,
        max_output_tokens=p.max_output_tokens,
    )


def to_response(outcome: Answered | Abstained) -> QuestionResponse:
    if isinstance(outcome, Abstained):
        return QuestionResponse(
            status="abstained",
            text=None,
            abstention_reason=outcome.reason,
            citations=[],
            uncited_statements=[],
            documents=_documents(outcome.context),
            truncated=False,
            provenance=_provenance(outcome.answer),
        )
    verified = outcome.verified
    return QuestionResponse(
        status="answered",
        text=verified.text,
        abstention_reason=None,
        citations=[_citation(c) for c in verified.citations],
        uncited_statements=[s.text for s in verified.uncited],
        documents=_documents(verified.answer.context),
        truncated=verified.answer.truncated,
        provenance=_provenance(verified.answer),
    )


@router.post(
    "",
    summary="Preguntar a mis documentos",
    responses=error_responses(409, 422, 429, 503),
)
def ask_question(
    body: QuestionRequest,
    user: CurrentUser,
    session: SessionDep,
    embedder: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
    llm: Annotated[LLMProvider, Depends(get_llm_provider)],
    limits: Annotated[SearchLimits, Depends(get_search_limits)],
) -> QuestionResponse:
    """Responde con fragmentos de mis documentos listos (READY) y cita sus fuentes.

    Sin evidencia suficiente responde 200 con `status: "abstained"` y sin texto; un fallo del
    proveedor de lenguaje es un 503 `llm_unavailable`, que no es lo mismo que abstenerse.

    Si el usuario agotó su cuota diaria (preguntas o tokens) responde 429 `usage_limit_exceeded`
    sin llamar a ningún proveedor. El consumo se anota (solo contadores) aunque la petición falle.
    """
    settings = get_settings()
    try:
        ensure_within_limits(
            session,
            user.id,
            UsageLimits(settings.usage_daily_questions, settings.usage_daily_tokens),
        )
    except UsageLimitExceeded as error:
        raise AppError(429, "usage_limit_exceeded", str(error)) from error

    tally = Tally()
    try:
        return _answer(body, user.id, session, embedder, llm, limits, tally)
    finally:
        record_usage(session, user.id, tally)


def _answer(
    body: QuestionRequest,
    owner_id: uuid.UUID,
    session: Session,
    embedder: EmbeddingProvider,
    llm: LLMProvider,
    limits: SearchLimits,
    tally: Tally,
) -> QuestionResponse:
    settings = get_settings()
    try:
        k, min_score = limits.resolve(None, None)
        try:
            question = prepare_question(
                body.question, embedder, max_chars=settings.question_max_chars
            )
        except EmbeddingError:
            tally.embedding_calls += 1
            raise
        tally.questions += 1
        tally.embedding_calls += 1
        ensure_index_compatible(session, question.embedding.spec, owner_id=owner_id)
        hits = search_chunks(
            session,
            question,
            owner_id=owner_id,
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
            "Tus documentos se indexaron con otro modelo de embeddings: reindéxalos.",
        ) from error
    except EmbeddingError as error:
        raise AppError(
            503, "embedding_unavailable", "No se pudo procesar la pregunta. Inténtalo más tarde."
        ) from error

    try:
        outcome = answer_question(
            session,
            question.text,
            hits,
            owner_id=owner_id,
            provider=llm,
            max_context_tokens=settings.answer_context_max_tokens,
        )
    except LLMError as error:
        tally.llm_calls += 1
        raise AppError(
            503, "llm_unavailable", "No se pudo generar la respuesta. Inténtalo más tarde."
        ) from error
    _add_model_usage(tally, question.text, outcome)
    return to_response(outcome)


def _add_model_usage(tally: Tally, question: str, outcome: Answered | Abstained) -> None:
    """Anota la llamada al modelo (si la hubo) con los tokens que informó, o una estimación."""
    answer = outcome.verified.answer if isinstance(outcome, Answered) else outcome.answer
    if answer is None:
        return
    completion = answer.completion
    tally.llm_calls += 1
    if completion.usage is not None:
        tally.input_tokens += completion.usage.input_tokens
        tally.output_tokens += completion.usage.output_tokens
    else:
        tally.input_tokens += estimate_tokens(question) + answer.context.tokens
        tally.output_tokens += estimate_tokens(completion.text)
