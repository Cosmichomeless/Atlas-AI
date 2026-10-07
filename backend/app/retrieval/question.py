"""Preparación de la pregunta: normalizada, acotada y vectorizada con el modelo del índice.

Una pregunta solo se compara con vectores de su misma especificación (modelo, dimensión y versión).
Antes de gastar una llamada al proveedor se comprueba que su dimensión cabe en el esquema y, antes
de buscar, que el índice del usuario no esté construido con otro modelo: en ese caso se avisa en
lugar de devolver cero resultados como si no hubiera nada relevante.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.embeddings.models import ChunkEmbedding
from app.embeddings.provider import Embedding, EmbeddingProvider, EmbeddingSpec
from app.embeddings.store import ensure_fits_schema
from app.features.documents.models import Document, DocumentChunk

_WHITESPACE = re.compile(r"\s+")
_HAS_CONTENT = re.compile(r"\w")


class InvalidQuestionError(ValueError):
    """La pregunta no se puede responder; `code` es estable y el mensaje, apto para el usuario."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IncompatibleIndexError(Exception):
    """El índice del usuario existe, pero con vectores de otro modelo, dimensión o versión."""

    def __init__(self, expected: EmbeddingSpec, indexed: list[str]) -> None:
        super().__init__(
            f"El índice está construido con {', '.join(indexed)} y la pregunta se vectoriza con "
            f"{expected.key}: reindexa los documentos o ajusta la configuración"
        )
        self.expected = expected
        self.indexed = indexed


@dataclass(frozen=True, slots=True)
class PreparedQuestion:
    """La pregunta ya normalizada y su vector, listos para buscar."""

    text: str
    embedding: Embedding


def _clean(char: str) -> str:
    """Los caracteres de formato invisibles se descartan y los demás de control son espacios."""
    category = unicodedata.category(char)
    if category == "Cf":
        return ""
    return " " if category.startswith("C") else char


def normalize_question(text: str, *, max_chars: int) -> str:
    """Texto canónico de la pregunta: Unicode NFC, sin controles y con espacios colapsados.

    Rechaza lo vacío (o sin ninguna letra o cifra) y lo que supera `max_chars` ya normalizado.
    """
    cleaned = "".join(_clean(char) for char in unicodedata.normalize("NFC", text))
    question = _WHITESPACE.sub(" ", cleaned).strip()
    if not _HAS_CONTENT.search(question):
        raise InvalidQuestionError("question_empty", "Escribe una pregunta.")
    if len(question) > max_chars:
        raise InvalidQuestionError(
            "question_too_long",
            f"La pregunta es demasiado larga: máximo {max_chars} caracteres.",
        )
    return question


def prepare_question(text: str, embedder: EmbeddingProvider, *, max_chars: int) -> PreparedQuestion:
    """Normaliza la pregunta y la vectoriza con `embedder` (el mismo que construye el índice).

    Lanza `InvalidQuestionError`, `IncompatibleDimensionsError` si la dimensión no cabe en la
    columna de vectores (antes de llamar al proveedor) o `EmbeddingError` si el proveedor falla.
    """
    question = normalize_question(text, max_chars=max_chars)
    ensure_fits_schema(embedder.spec)
    return PreparedQuestion(question, embedder.embed_one(question))


def ensure_index_compatible(session: Session, spec: EmbeddingSpec, *, owner_id: uuid.UUID) -> None:
    """Falla si el usuario tiene vectores pero ninguno es de `spec`.

    Un usuario sin vectores es compatible (no hay nada que buscar). Si el índice mezcla
    especificaciones —un reindexado a medias— se busca en las que coinciden.
    """
    rows = session.execute(
        select(
            ChunkEmbedding.provider,
            ChunkEmbedding.model,
            ChunkEmbedding.dimensions,
            ChunkEmbedding.version,
        )
        .join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.owner_id == owner_id)
        .distinct()
    ).all()
    indexed = sorted(EmbeddingSpec(*row).key for row in rows)
    if indexed and spec.key not in indexed:
        raise IncompatibleIndexError(spec, indexed)
