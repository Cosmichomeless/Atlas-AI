"""Responder o abstenerse: sin evidencia suficiente no se fabrica una respuesta.

`answer_question` recibe los fragmentos recuperados y devuelve `Answered` (respuesta con citas
verificadas) o `Abstained` (con el motivo). La abstención es un resultado normal, no un error: un
fallo del proveedor (`LLMError`) se propaga tal cual, de modo que quien llama (la API) puede
distinguir "no hay evidencia" de "el modelo no está disponible".

Motivos de abstención, de más barato a más caro de detectar:

- `no_relevant_chunks`: no hay fragmentos (o ninguno cabe en el contexto). No se llama al modelo.
- `insufficient_evidence`: el modelo declara que los fragmentos no bastan (`SIN_EVIDENCIA`).
- `no_valid_citations`: tras verificar, ninguna cita respalda la respuesta; un texto sin fuente
  comprobable no se presenta como respuesta de los documentos.
"""

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.answers.citations import VerifiedAnswer, verify_citations
from app.answers.context import BoundedContext, build_context
from app.answers.generate import Answer, EmptyContextError, generate_answer
from app.answers.prompt import INSUFFICIENT_MARKER
from app.embeddings.store import SimilarChunk
from app.llm.provider import LLMProvider

logger = logging.getLogger("app.answers")

AbstentionReason = Literal["no_relevant_chunks", "insufficient_evidence", "no_valid_citations"]

_MARKER = re.compile(rf"\b{re.escape(INSUFFICIENT_MARKER)}\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Answered:
    """Respuesta fundamentada: texto sin citas inválidas y al menos una cita verificada."""

    verified: VerifiedAnswer


@dataclass(frozen=True, slots=True)
class Abstained:
    """No se responde. `answer` es lo que dijo el modelo, si se le llegó a preguntar.

    Nunca se entrega como respuesta: queda solo para diagnóstico. `context` conserva los
    documentos consultados.
    """

    question: str
    reason: AbstentionReason
    context: BoundedContext
    answer: Answer | None = None


Outcome = Answered | Abstained


def declares_insufficient(text: str) -> bool:
    """El modelo dice que no hay evidencia: el marcador aparece en cualquier parte del texto.

    Es deliberadamente estricto: una respuesta que mezcla el marcador con otro contenido no es
    fiable, y entregarla (marcador incluido) sería peor que abstenerse.
    """
    return _MARKER.search(text) is not None


def _abstain(
    question: str, reason: AbstentionReason, context: BoundedContext, answer: Answer | None = None
) -> Abstained:
    logger.info(
        "answer abstained reason=%s chunks=%d called_model=%s",
        reason,
        len(context.items),
        answer is not None,
    )
    return Abstained(question, reason, context, answer)


def answer_question(
    session: Session,
    question: str,
    hits: Sequence[SimilarChunk],
    *,
    owner_id: uuid.UUID,
    provider: LLMProvider,
    max_context_tokens: int,
) -> Outcome:
    """Responde `question` con `hits` o se abstiene; `LLMError` si el proveedor falla."""
    context = build_context(hits, max_tokens=max_context_tokens)
    try:
        answer = generate_answer(question, context, provider)
    except EmptyContextError:
        return _abstain(question, "no_relevant_chunks", context)

    if declares_insufficient(answer.text):
        return _abstain(question, "insufficient_evidence", context, answer)

    verified = verify_citations(session, answer, owner_id=owner_id)
    if not verified.citations:
        return _abstain(question, "no_valid_citations", context, answer)
    return Answered(verified)
