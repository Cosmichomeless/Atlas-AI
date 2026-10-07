import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.types import created_at_column, uuid_pk

# Dimensión de la columna `embedding`. Cambiarla exige una migración y reindexar todo; una
# `EMBEDDING_DIMENSIONS` distinta se rechaza al guardar (ver `store.save_embeddings`).
VECTOR_DIMENSIONS = 1536


class ChunkEmbedding(Base):
    """Vector de un fragmento junto con la especificación que lo generó.

    `chunk_id` conserva el vínculo con el fragmento (y, por él, con el documento y su propietario),
    así una búsqueda por similitud siempre puede citar el origen. Proveedor, modelo, dimensión y
    versión viajan con el vector: solo son comparables los de la misma especificación, y varias
    pueden convivir para un fragmento mientras se migra de modelo. Borrar el fragmento borra sus
    vectores.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "provider", "model", "dimensions", "version"),
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        CheckConstraint("dimensions = vector_dims(embedding)", name="dimensions_match_vector"),
        # Coseno: los proveedores devuelven vectores normalizados y es la métrica habitual.
        Index(
            "ix_chunk_embeddings_embedding_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(VECTOR_DIMENSIONS), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
