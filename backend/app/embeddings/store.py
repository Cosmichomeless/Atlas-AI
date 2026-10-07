"""Persistencia y búsqueda de vectores de fragmentos en pgvector."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, delete, or_, select
from sqlalchemy.orm import Session

from app.embeddings.models import VECTOR_DIMENSIONS, ChunkEmbedding
from app.embeddings.provider import Embedding, EmbeddingError, EmbeddingSpec
from app.features.documents.models import Document, DocumentChunk


class IncompatibleDimensionsError(EmbeddingError):
    """El proveedor produce una dimensión que la columna de vectores no admite (configuración)."""


@dataclass(frozen=True, slots=True)
class SimilarChunk:
    """Fragmento encontrado, con su documento (para citarlo) y su distancia coseno (0 = igual)."""

    chunk: DocumentChunk
    document: Document
    distance: float

    @property
    def score(self) -> float:
        """Similitud coseno en [-1, 1] (1 = misma dirección): lo contrario de la distancia."""
        return max(-1.0, min(1.0, 1.0 - self.distance))


def ensure_fits_schema(spec: EmbeddingSpec) -> None:
    """La columna tiene una dimensión fija: rechaza cualquier otra antes de tocar la base."""
    if spec.dimensions != VECTOR_DIMENSIONS:
        raise IncompatibleDimensionsError(
            f"La columna de vectores admite {VECTOR_DIMENSIONS} dimensiones y {spec.key} produce "
            f"{spec.dimensions}: ajusta EMBEDDING_DIMENSIONS o migra y reindexa"
        )


def _same_spec(spec: EmbeddingSpec) -> ColumnElement[bool]:
    return and_(
        ChunkEmbedding.provider == spec.provider,
        ChunkEmbedding.model == spec.model,
        ChunkEmbedding.dimensions == spec.dimensions,
        ChunkEmbedding.version == spec.version,
    )


def save_embeddings(
    session: Session, chunks: Sequence[DocumentChunk], embeddings: Sequence[Embedding]
) -> None:
    """Guarda `embeddings[i]` para `chunks[i]` en la transacción actual; el llamador confirma.

    Si un fragmento ya tenía un vector de la misma especificación, se sustituye (sin duplicados);
    los de otras especificaciones se conservan. Valida cantidad y dimensión antes de escribir.
    """
    if len(chunks) != len(embeddings):
        raise EmbeddingError(f"{len(embeddings)} vectores para {len(chunks)} fragmentos")
    for embedding in embeddings:
        ensure_fits_schema(embedding.spec)
    if not chunks:
        return

    chunk_ids = [chunk.id for chunk in chunks]
    specs = {embedding.spec for embedding in embeddings}
    session.execute(
        delete(ChunkEmbedding).where(
            ChunkEmbedding.chunk_id.in_(chunk_ids), or_(*(_same_spec(spec) for spec in specs))
        )
    )
    session.add_all(
        ChunkEmbedding(
            chunk_id=chunk.id,
            provider=embedding.spec.provider,
            model=embedding.spec.model,
            dimensions=embedding.spec.dimensions,
            version=embedding.spec.version,
            embedding=list(embedding.vector),
        )
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    )
    session.flush()


def nearest_chunks(
    session: Session,
    query: Embedding,
    *,
    owner_id: uuid.UUID,
    limit: int = 5,
    max_distance: float | None = None,
    document_ids: Collection[uuid.UUID] | None = None,
) -> list[SimilarChunk]:
    """Fragmentos del propietario más parecidos a `query`, solo entre vectores de su misma spec.

    Filtra siempre por propietario: es la barrera que impide recuperar documentos ajenos. Con
    `max_distance` descarta, en la propia consulta, lo que queda más lejos de esa distancia coseno.
    `document_ids` restringe la búsqueda a esos documentos —también dentro de la consulta, para que
    el límite se aplique ya filtrado—; los que no son del propietario simplemente no coinciden, sin
    distinguirse de los inexistentes. Una colección vacía no busca en ningún documento.
    """
    ensure_fits_schema(query.spec)
    distance = ChunkEmbedding.embedding.cosine_distance(list(query.vector))
    statement = (
        select(DocumentChunk, Document, distance.label("distance"))
        .join(ChunkEmbedding, ChunkEmbedding.chunk_id == DocumentChunk.id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.owner_id == owner_id, _same_spec(query.spec))
        .order_by(distance, DocumentChunk.document_id, DocumentChunk.ordinal)
        .limit(limit)
    )
    if max_distance is not None:
        statement = statement.where(distance <= max_distance)
    if document_ids is not None:
        statement = statement.where(DocumentChunk.document_id.in_(document_ids))
    rows = session.execute(statement).all()
    return [SimilarChunk(chunk, document, float(dist)) for chunk, document, dist in rows]
