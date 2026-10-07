"""Reducción de resultados redundantes: repetidos y contiguos que apenas aportan texto nuevo.

La política es una función pura del orden y del texto de los resultados, así que es determinista
y se puede medir: `reduce_redundancy` devuelve qué se descarta y por qué, y la tasa de redundancia
resultante sirve de métrica en una evaluación.

Un resultado (de peor a mejor puesto) se descarta si, frente a uno ya conservado **del mismo
documento**, ocurre alguna de estas dos cosas:

- `exact`: tiene el mismo texto (normalizado) —p. ej. una cabecera repetida en cada página—;
- `contiguous`: sus fragmentos son vecinos (ordenales a `window` o menos) y comparten al menos
  `min_overlap` de sus secuencias de palabras: el solapamiento del troceado o texto casi igual.

Fragmentos de documentos distintos nunca se fusionan: son fuentes distintas aunque coincidan.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.embeddings.store import SimilarChunk

Reason = Literal["exact", "contiguous"]

_WORD = re.compile(r"\w+")
SHINGLE_WORDS = 3


@dataclass(frozen=True, slots=True)
class DedupPolicy:
    """Parámetros de la reducción; `overfetch` es cuántos candidatos se piden por cada resultado."""

    min_overlap: float = 0.5
    window: int = 1
    overfetch: int = 3

    def __post_init__(self) -> None:
        if not 0.0 < self.min_overlap <= 1.0:
            raise ValueError("min_overlap debe estar en (0, 1]")
        if self.window < 0:
            raise ValueError("window no puede ser negativo")
        if self.overfetch < 1:
            raise ValueError("overfetch debe ser al menos 1")


@dataclass(frozen=True, slots=True)
class Dropped:
    """Un resultado descartado, el conservado al que repite y por qué."""

    hit: SimilarChunk
    kept: SimilarChunk
    reason: Reason
    overlap: float


@dataclass(frozen=True, slots=True)
class Reduction:
    kept: list[SimilarChunk]
    dropped: list[Dropped] = field(default_factory=list)

    @property
    def redundancy_rate(self) -> float:
        """Fracción de los resultados considerados que eran redundantes (0 = ninguno)."""
        total = len(self.kept) + len(self.dropped)
        return len(self.dropped) / total if total else 0.0


def normalize(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def shingles(text: str) -> frozenset[tuple[str, ...]]:
    """Secuencias de `SHINGLE_WORDS` palabras consecutivas (o el texto entero si es más corto)."""
    words = _WORD.findall(text.lower())
    if not words:
        return frozenset()
    size = min(SHINGLE_WORDS, len(words))
    return frozenset(tuple(words[i : i + size]) for i in range(len(words) - size + 1))


def overlap(first: str, second: str) -> float:
    """Parte de las secuencias de palabras del texto más corto que también están en el otro."""
    a, b = shingles(first), shingles(second)
    smaller = min(len(a), len(b))
    return len(a & b) / smaller if smaller else 0.0


def _redundant_with(
    hit: SimilarChunk, kept: SimilarChunk, policy: DedupPolicy
) -> tuple[Reason, float] | None:
    if hit.document.id != kept.document.id:
        return None
    if normalize(hit.chunk.text) == normalize(kept.chunk.text):
        return "exact", 1.0
    if abs(hit.chunk.ordinal - kept.chunk.ordinal) <= policy.window:
        shared = overlap(hit.chunk.text, kept.chunk.text)
        if shared >= policy.min_overlap:
            return "contiguous", shared
    return None


def reduce_redundancy(hits: Sequence[SimilarChunk], policy: DedupPolicy) -> Reduction:
    """Conserva los mejores puestos y descarta los que repiten a uno mejor ya conservado."""
    kept: list[SimilarChunk] = []
    dropped: list[Dropped] = []
    for hit in hits:
        for better in kept:
            verdict = _redundant_with(hit, better, policy)
            if verdict is not None:
                dropped.append(Dropped(hit, better, verdict[0], verdict[1]))
                break
        else:
            kept.append(hit)
    return Reduction(kept, dropped)
