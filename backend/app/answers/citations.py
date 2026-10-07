"""Verificación de citas: solo se entregan las que apuntan a una fuente real del usuario.

El modelo cita con etiquetas (`[S1]`) que `build_context` asignó a fragmentos concretos. Una
etiqueta que no existe en el contexto, o cuyo fragmento ya no está en la base con el mismo
documento y la misma ubicación, es una cita inventada u obsoleta: se rechaza, se quita del texto
entregado y la frase que la usaba deja de contar como respaldada.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.answers.context import ContextItem
from app.answers.generate import Answer
from app.answers.grounding import Statement, extract_labels, split_statements, strip_labels
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus

Reason = Literal["unknown_label", "chunk_missing", "source_changed"]


@dataclass(frozen=True, slots=True)
class Citation:
    """Una cita verificada: documento y ubicación exacta del fragmento que respalda la frase."""

    label: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    ordinal: int
    page: int | None
    section: str | None
    start_line: int | None
    end_line: int | None

    @classmethod
    def from_item(cls, item: ContextItem) -> "Citation":
        return cls(
            item.label,
            item.chunk_id,
            item.document_id,
            item.filename,
            item.ordinal,
            item.page,
            item.section,
            item.start_line,
            item.end_line,
        )


@dataclass(frozen=True, slots=True)
class RejectedCitation:
    """Etiqueta que el modelo usó y que no se pudo comprobar; `reason` dice por qué."""

    label: str
    reason: Reason


@dataclass(frozen=True, slots=True)
class VerifiedAnswer:
    """La respuesta tal y como puede entregarse: sin citas inválidas y con las válidas resueltas.

    `text` es el texto del modelo sin las etiquetas rechazadas (`raw_text` lo conserva intacto);
    `statements` se recalcula sobre `text`, de modo que una frase apoyada solo en una cita
    rechazada pasa a `uncited`.
    """

    answer: Answer
    raw_text: str
    text: str
    citations: tuple[Citation, ...]
    rejected: tuple[RejectedCitation, ...]
    statements: tuple[Statement, ...]

    @property
    def uncited(self) -> tuple[Statement, ...]:
        return tuple(s for s in self.statements if s.kind == "uncited")


def _matches(item: ContextItem, chunk: DocumentChunk) -> bool:
    return (
        chunk.document_id == item.document_id
        and chunk.ordinal == item.ordinal
        and chunk.page == item.page
        and chunk.section == item.section
        and chunk.start_line == item.start_line
        and chunk.end_line == item.end_line
    )


def verify_citations(session: Session, answer: Answer, *, owner_id: uuid.UUID) -> VerifiedAnswer:
    """Comprueba cada etiqueta citada: debe estar en el contexto enviado y su fragmento debe
    seguir existiendo, ser de un documento READY de `owner_id` y tener la misma ubicación."""
    labels = extract_labels(answer.text)
    items = {label: answer.context.item(label) for label in labels}
    known = [item for item in items.values() if item is not None]

    stored: dict[uuid.UUID, DocumentChunk] = {}
    if known:
        rows = session.execute(
            select(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.id.in_([item.chunk_id for item in known]),
                Document.owner_id == owner_id,
                Document.status == DocumentStatus.READY,
            )
        )
        stored = {chunk.id: chunk for chunk in rows.scalars()}

    citations: list[Citation] = []
    rejected: list[RejectedCitation] = []
    for label in labels:
        item = items[label]
        if item is None:
            rejected.append(RejectedCitation(label, "unknown_label"))
        elif (chunk := stored.get(item.chunk_id)) is None:
            rejected.append(RejectedCitation(label, "chunk_missing"))
        elif not _matches(item, chunk):
            rejected.append(RejectedCitation(label, "source_changed"))
        else:
            citations.append(Citation.from_item(item))

    text = strip_labels(answer.text, [r.label for r in rejected])
    return VerifiedAnswer(
        answer, answer.text, text, tuple(citations), tuple(rejected), split_statements(text)
    )
