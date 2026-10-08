"""Contrato común de los proveedores de embeddings.

Cada vector sale acompañado de la `EmbeddingSpec` que lo produjo (proveedor, modelo, dimensión y
versión). Vectores de especificaciones distintas no son comparables entre sí, así que quien los
guarde debe conservar la especificación junto al vector.
"""

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass


class EmbeddingError(Exception):
    """El proveedor no pudo producir los vectores; el mensaje es seguro de registrar.

    `transient` indica que reintentar más tarde puede funcionar (red, límite de tasa, caída del
    servicio) y es lo que se asume por defecto; `False` se reserva a lo que esperar no arregla
    (clave inválida, petición rechazada, respuesta incomprensible).
    """

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    """Qué espacio vectorial produce un proveedor.

    `version` identifica el comportamiento del adaptador (preprocesado, normalización…): se
    incrementa cuando los vectores de un mismo modelo dejarían de ser comparables con los antiguos.
    """

    provider: str
    model: str
    dimensions: int
    version: str

    def __post_init__(self) -> None:
        if not (self.provider and self.model and self.version):
            raise ValueError("proveedor, modelo y versión son obligatorios")
        if self.dimensions <= 0:
            raise ValueError("la dimensión debe ser positiva")

    @property
    def key(self) -> str:
        """Identificador estable, p. ej. `openai/text-embedding-3-small/1536/v1`."""
        return f"{self.provider}/{self.model}/{self.dimensions}/v{self.version}"


@dataclass(frozen=True, slots=True)
class Embedding:
    """Un vector y la especificación que lo generó."""

    vector: tuple[float, ...]
    spec: EmbeddingSpec


class EmbeddingProvider(ABC):
    """Base de los adaptadores: `embed` valida lo que devuelve cada implementación."""

    spec: EmbeddingSpec
    max_batch_size: int = 64

    @abstractmethod
    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectores de un lote, en el mismo orden que `texts`."""

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Vectoriza los textos, en lotes, conservando el orden.

        Lanza `EmbeddingError` si algún texto está en blanco o si el proveedor devuelve un número
        de vectores o una dimensión distintos de los esperados.
        """
        if any(not text.strip() for text in texts):
            raise EmbeddingError("No se puede vectorizar un texto en blanco")
        embeddings: list[Embedding] = []
        for start in range(0, len(texts), self.max_batch_size):
            batch = texts[start : start + self.max_batch_size]
            vectors = self._embed_batch(batch)
            if len(vectors) != len(batch):
                raise EmbeddingError(
                    f"El proveedor devolvió {len(vectors)} vectores para {len(batch)} textos"
                )
            embeddings.extend(Embedding(self._checked(vector), self.spec) for vector in vectors)
        return embeddings

    def embed_one(self, text: str) -> Embedding:
        return self.embed([text])[0]

    def _checked(self, vector: Sequence[float]) -> tuple[float, ...]:
        if len(vector) != self.spec.dimensions:
            raise EmbeddingError(
                f"Dimensión {len(vector)} distinta de la esperada ({self.spec.dimensions}) "
                f"para {self.spec.key}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingError("El proveedor devolvió valores no finitos")
        return tuple(float(value) for value in vector)
