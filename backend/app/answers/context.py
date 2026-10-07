"""Contexto acotado: los fragmentos recuperados que caben en un presupuesto explícito de tokens.

Cada fragmento conserva su identificador (`chunk_id`) y su referencia de fuente (documento,
página, sección, líneas) y recibe una etiqueta estable (`S1`, `S2`…, en orden de relevancia) con
la que el modelo puede citarlo y con la que luego se verifica la cita.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.answers.tokens import estimate_tokens
from app.embeddings.store import SimilarChunk

FILENAME_MAX_CHARS = 80
SECTION_MAX_CHARS = 160
SEPARATOR = "\n\n"


def label_for(position: int) -> str:
    """Etiqueta de la fuente en la posición `position` (0 → `S1`)."""
    return f"S{position + 1}"


def _clip(value: str, limit: int) -> str:
    one_line = re.sub(r"\s+", " ", value).strip()
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class ContextItem:
    """Un fragmento incluido en el contexto, con todo lo necesario para citarlo."""

    label: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    ordinal: int
    page: int | None
    section: str | None
    start_line: int | None
    end_line: int | None
    score: float
    text: str

    @property
    def reference(self) -> str:
        """Referencia legible de la fuente, en una línea."""
        parts = [_clip(self.filename, FILENAME_MAX_CHARS)]
        if self.page is not None:
            parts.append(f"p. {self.page}")
        if self.section:
            parts.append(_clip(self.section, SECTION_MAX_CHARS))
        if self.start_line is not None:
            parts.append(f"líneas {self.start_line}-{self.end_line}")
        return " — ".join(parts)

    @property
    def block(self) -> str:
        """El fragmento tal y como se envía al modelo: cabecera con etiqueta y texto."""
        return f"[{self.label}] {self.reference}\n{self.text}"

    @classmethod
    def from_hit(cls, label: str, hit: SimilarChunk) -> "ContextItem":
        chunk, document = hit.chunk, hit.document
        return cls(
            label=label,
            chunk_id=chunk.id,
            document_id=document.id,
            filename=document.filename,
            ordinal=chunk.ordinal,
            page=chunk.page,
            section=chunk.section,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            score=hit.score,
            text=chunk.text,
        )


@dataclass(frozen=True, slots=True)
class Omitted:
    """Un fragmento que no entró en el contexto, y por qué."""

    chunk_id: uuid.UUID
    reason: str  # "over_budget" | "duplicate"


@dataclass(frozen=True, slots=True)
class BoundedContext:
    """Fragmentos elegidos, su texto final y la cuenta de tokens frente al límite."""

    items: tuple[ContextItem, ...]
    max_tokens: int
    omitted: tuple[Omitted, ...] = ()

    @property
    def text(self) -> str:
        return SEPARATOR.join(item.block for item in self.items)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def empty(self) -> bool:
        return not self.items

    def item(self, label: str) -> ContextItem | None:
        """El fragmento con esa etiqueta, o `None` si el contexto no la contiene."""
        return next((item for item in self.items if item.label == label), None)


def build_context(hits: Sequence[SimilarChunk], *, max_tokens: int) -> BoundedContext:
    """Elige, por orden de relevancia, los fragmentos que caben en `max_tokens`.

    El texto de un fragmento nunca se recorta: o entra entero o se omite (y queda en `omitted`),
    y se siguen probando los siguientes, que pueden ser más cortos. Un mismo fragmento no entra
    dos veces. El total se mide sobre el texto final, de modo que `tokens <= max_tokens` siempre.
    """
    if max_tokens < 1:
        raise ValueError("max_tokens debe ser al menos 1")

    items: list[ContextItem] = []
    omitted: list[Omitted] = []
    seen: set[uuid.UUID] = set()
    for hit in hits:
        if hit.chunk.id in seen:
            omitted.append(Omitted(hit.chunk.id, "duplicate"))
            continue
        seen.add(hit.chunk.id)
        candidate = ContextItem.from_hit(label_for(len(items)), hit)
        text = SEPARATOR.join(item.block for item in [*items, candidate])
        if estimate_tokens(text) > max_tokens:
            omitted.append(Omitted(hit.chunk.id, "over_budget"))
            continue
        items.append(candidate)
    return BoundedContext(tuple(items), max_tokens, tuple(omitted))
