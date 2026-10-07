"""Genera una respuesta fundamentada en el contexto y deja constancia de cómo se produjo."""

import logging
import uuid
from dataclasses import dataclass

from app.answers.context import BoundedContext
from app.answers.grounding import Statement, split_statements
from app.answers.prompt import PROMPT_FINGERPRINT, PROMPT_VERSION, build_messages
from app.llm.provider import Completion, LLMProvider

logger = logging.getLogger("app.answers")


class EmptyContextError(ValueError):
    """No hay fragmentos con los que responder: sin contexto no se llama al modelo."""


@dataclass(frozen=True, slots=True)
class Provenance:
    """Lo necesario para reproducir una respuesta: prompt, modelo, parámetros y fuentes."""

    prompt_version: str
    prompt_fingerprint: str
    llm: str
    temperature: float
    max_output_tokens: int
    context_tokens: int
    chunk_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class Answer:
    """Respuesta del modelo con su contexto, su procedencia y sus afirmaciones clasificadas."""

    question: str
    text: str
    context: BoundedContext
    completion: Completion
    provenance: Provenance
    statements: tuple[Statement, ...]

    @property
    def uncited(self) -> tuple[Statement, ...]:
        """Afirmaciones sin fuente ni marca de ajenas: no pueden presentarse como del documento."""
        return tuple(s for s in self.statements if s.kind == "uncited")

    @property
    def truncated(self) -> bool:
        return self.completion.truncated


def generate_answer(question: str, context: BoundedContext, provider: LLMProvider) -> Answer:
    """Pregunta al modelo usando solo `context`.

    Lanza `EmptyContextError` si el contexto está vacío y `LLMError` si el proveedor falla. La
    versión del prompt, el modelo y los parámetros quedan en `Answer.provenance` y en el registro.
    """
    if context.empty:
        raise EmptyContextError("El contexto está vacío: no hay con qué fundamentar una respuesta")

    completion = provider.complete(build_messages(question, context))
    text = completion.text.strip()
    provenance = Provenance(
        prompt_version=PROMPT_VERSION,
        prompt_fingerprint=PROMPT_FINGERPRINT,
        llm=completion.spec.key,
        temperature=completion.params.temperature,
        max_output_tokens=completion.params.max_output_tokens,
        context_tokens=context.tokens,
        chunk_ids=tuple(item.chunk_id for item in context.items),
    )
    logger.info(
        "answer generated prompt=%s fingerprint=%s llm=%s temperature=%s max_output_tokens=%s "
        "context_tokens=%s chunks=%d truncated=%s",
        provenance.prompt_version,
        provenance.prompt_fingerprint,
        provenance.llm,
        provenance.temperature,
        provenance.max_output_tokens,
        provenance.context_tokens,
        len(provenance.chunk_ids),
        completion.truncated,
    )
    return Answer(question, text, context, completion, provenance, split_statements(text))
