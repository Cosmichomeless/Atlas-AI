"""Reordenación opcional de los candidatos: similitud vectorial más solape léxico con la pregunta.

La búsqueda vectorial ordena por cercanía semántica y puede dejar fuera del top-k un fragmento que
contiene justo las palabras de la pregunta. Con una política activa, tras reducir la redundancia se
reordenan todos los candidatos con una puntuación combinada y se recorta a `k`:

    combinada = (1 - weight) * similitud_vectorial + weight * solape_léxico

El solape léxico es la fracción de las palabras con contenido de la pregunta (sin las vacías, con
una raíz de `STEM_CHARS` letras para tolerar flexiones) que aparecen en el fragmento. Es una
función pura del texto: determinista, sin red ni coste de inferencia. Si algún día se quiere un
modelo de reordenación, basta con otra estrategia con la misma forma de entrada y salida.

La puntuación de cada `SimilarChunk` sigue siendo la vectorial: el reranking solo cambia el orden, y
`Reranking.entries` conserva puesto original, puntuaciones y puesto final para poder compararlos.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.embeddings.store import SimilarChunk

Strategy = Literal["lexical"]

STEM_CHARS = 6
MIN_WORD_CHARS = 4

_WORD = re.compile(r"\w+")
_STOPWORDS = frozenset(
    [
        "cual",
        "cuales",
        "cuanto",
        "cuantos",
        "cuanta",
        "cuantas",
        "como",
        "donde",
        "cuando",
        "quien",
        "quienes",
        "que",
        "para",
        "pero",
        "porque",
        "este",
        "esta",
        "estos",
        "estas",
        "ese",
        "esa",
        "esos",
        "esas",
        "aquel",
        "sobre",
        "entre",
        "desde",
        "hasta",
        "hacia",
        "segun",
        "tiene",
        "tienen",
        "puede",
        "pueden",
        "debe",
        "deben",
        "hay",
        "son",
        "ser",
        "estan",
        "fue",
        "era",
        "sido",
        "tambien",
        "mas",
        "muy",
        "con",
        "sin",
        "por",
        "los",
        "las",
        "del",
        "una",
        "unos",
        "unas",
        "nos",
        "les",
        "mis",
        "tus",
        "sus",
    ]
)


@dataclass(frozen=True, slots=True)
class RerankPolicy:
    """Parámetros del reranking; `pool` es cuántos candidatos se piden por cada resultado."""

    strategy: Strategy = "lexical"
    weight: float = 0.5
    pool: int = 3
    version: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("weight debe estar en [0, 1]")
        if self.pool < 1:
            raise ValueError("pool debe ser al menos 1")

    @property
    def key(self) -> str:
        return f"{self.strategy}/v{self.version}"

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "strategy": self.strategy,
            "weight": self.weight,
            "pool": self.pool,
        }


@dataclass(frozen=True, slots=True)
class RerankEntry:
    """Qué le pasó a un candidato: de qué puesto salió, con qué puntuaciones y dónde quedó."""

    chunk_id: str
    filename: str
    ordinal: int
    original_rank: int
    final_rank: int
    vector_score: float
    lexical_score: float
    combined_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "filename": self.filename,
            "ordinal": self.ordinal,
            "original_rank": self.original_rank,
            "final_rank": self.final_rank,
            "vector_score": round(self.vector_score, 6),
            "lexical_score": round(self.lexical_score, 6),
            "combined_score": round(self.combined_score, 6),
        }


@dataclass(frozen=True, slots=True)
class Reranking:
    """Candidatos reordenados (todos) y el detalle de cada uno, en el orden final."""

    ordered: list[SimilarChunk]
    entries: tuple[RerankEntry, ...]

    @property
    def moved(self) -> int:
        """Cuántos candidatos cambiaron de puesto."""
        return sum(1 for e in self.entries if e.original_rank != e.final_rank)


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def stems(text: str) -> frozenset[str]:
    """Raíces de las palabras con contenido (sin tildes, sin vacías, números enteros)."""
    return frozenset(
        word if any(ch.isdigit() for ch in word) else word[:STEM_CHARS]
        for word in _WORD.findall(_plain(text))
        if word not in _STOPWORDS
        and (len(word) >= MIN_WORD_CHARS or any(c.isdigit() for c in word))
    )


def lexical_score(question: frozenset[str], text: str) -> float:
    """Fracción de las raíces de la pregunta presentes en `text` (0 si la pregunta no tiene)."""
    if not question:
        return 0.0
    return len(question & stems(text)) / len(question)


def rerank(
    question_text: str, candidates: Sequence[SimilarChunk], policy: RerankPolicy
) -> Reranking:
    """Reordena `candidates` (ya de mejor a peor vectorialmente) con la puntuación combinada.

    El orden es estable: a igual puntuación combinada se conserva el puesto original, así que el
    resultado es determinista.
    """
    wanted = stems(question_text)
    scored = [
        (
            (1.0 - policy.weight) * hit.score
            + policy.weight * lexical_score(wanted, hit.chunk.text),
            lexical_score(wanted, hit.chunk.text),
            position,
            hit,
        )
        for position, hit in enumerate(candidates, start=1)
    ]
    scored.sort(key=lambda item: (-item[0], item[2]))
    entries = tuple(
        RerankEntry(
            chunk_id=str(hit.chunk.id),
            filename=hit.document.filename,
            ordinal=hit.chunk.ordinal,
            original_rank=position,
            final_rank=final,
            vector_score=hit.score,
            lexical_score=lexical,
            combined_score=combined,
        )
        for final, (combined, lexical, position, hit) in enumerate(scored, start=1)
    )
    return Reranking([item[3] for item in scored], entries)
