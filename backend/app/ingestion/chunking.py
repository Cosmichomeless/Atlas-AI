"""Política de fragmentación: cómo se parte el texto extraído para indexarlo.

Decisiones (todas reproducibles: la misma entrada y política dan siempre los mismos fragmentos):

- **Tamaño** (`CHUNK_SIZE_CHARS`, 1000 por defecto, ~200-250 tokens): cabe de sobra en el límite de
  cualquier modelo de embeddings, y es lo bastante corto para que un fragmento trate de una sola
  idea (mejor precisión al recuperar) y lo bastante largo para llevar contexto suficiente al LLM.
- **Solape** (`CHUNK_OVERLAP_CHARS`, 150 por defecto, 15 %): una frase partida en el límite aparece
  entera en uno de los dos fragmentos, sin duplicar apenas almacenamiento. Nunca supera la mitad
  del tamaño, así cada fragmento aporta contenido nuevo y el proceso siempre avanza.
- **Fronteras**: un fragmento nunca cruza una página (PDF) ni una sección (Markdown), para que su
  cita sea exacta. Dentro de ellas se corta, por este orden de preferencia, en fin de párrafo, de
  línea, de frase y de palabra; solo un texto sin espacios se corta en seco.
- **Sin vacíos y en orden**: no se emiten fragmentos en blanco y `ordinal` sigue el orden del
  documento (0, 1, 2…).
"""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.features.documents.extraction import ExtractedBlock

# Súbela al cambiar el algoritmo de corte: los documentos indexados con otra versión quedan
# obsoletos aunque el tamaño y el solape no cambien (ver `app.ingestion.reindex`).
CHUNKER_VERSION = 1

_SEPARATOR = "\n\n"
# De más a menos preferido; el corte queda justo después del separador.
_BREAKS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


@dataclass(frozen=True)
class ChunkPolicy:
    size: int
    overlap: int

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("El tamaño del fragmento debe ser positivo")
        if not 0 <= self.overlap <= self.size // 2:
            raise ValueError("El solape debe estar entre 0 y la mitad del tamaño")

    @property
    def key(self) -> str:
        """Huella de la política: lo que debe coincidir para que dos índices sean comparables."""
        return f"v{CHUNKER_VERSION}:size={self.size},overlap={self.overlap}"

    @classmethod
    def from_settings(cls, settings: Settings) -> "ChunkPolicy":
        return cls(settings.chunk_size_chars, settings.chunk_overlap_chars)


@dataclass(frozen=True)
class Chunk:
    """Fragmento listo para indexar y rastreable hasta su origen."""

    document_id: uuid.UUID
    ordinal: int
    text: str
    page: int | None = None
    section: str | None = None
    start_line: int | None = None
    end_line: int | None = None


def chunk_blocks(blocks: Sequence[ExtractedBlock], policy: ChunkPolicy) -> list[Chunk]:
    """Convierte los bloques extraídos (en orden) en fragmentos numerados."""
    chunks: list[Chunk] = []
    for group in _groups(blocks):
        text, spans = _join(group)
        for start, end in _windows(text, policy):
            first = group[_block_at(spans, start)]
            chunks.append(
                Chunk(
                    document_id=first.document_id,
                    ordinal=len(chunks),
                    text=text[start:end],
                    page=first.page,
                    section=first.section,
                    start_line=_line_at(text, spans, group, start, first=True),
                    end_line=_line_at(text, spans, group, end - 1, first=False),
                )
            )
    return chunks


def _groups(blocks: Sequence[ExtractedBlock]) -> Iterator[list[ExtractedBlock]]:
    """Bloques consecutivos de la misma página y sección: el mínimo que un fragmento no cruza."""
    group: list[ExtractedBlock] = []
    for block in blocks:
        if not block.text.strip():
            continue
        if group and (block.page, block.section, block.document_id) != (
            group[0].page,
            group[0].section,
            group[0].document_id,
        ):
            yield group
            group = []
        group.append(block)
    if group:
        yield group


def _join(group: list[ExtractedBlock]) -> tuple[str, list[tuple[int, int]]]:
    """Texto del grupo y el rango de caracteres que ocupa cada bloque dentro de él."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    position = 0
    for block in group:
        if parts:
            position += len(_SEPARATOR)
        spans.append((position, position + len(block.text)))
        parts.append(block.text)
        position += len(block.text)
    return _SEPARATOR.join(parts), spans


def _block_at(spans: list[tuple[int, int]], position: int) -> int:
    """Índice del bloque que contiene `position` (o el siguiente si cae en un separador)."""
    for index, (_, end) in enumerate(spans):
        if position < end:
            return index
    return len(spans) - 1


def _line_at(
    text: str,
    spans: list[tuple[int, int]],
    group: list[ExtractedBlock],
    position: int,
    *,
    first: bool,
) -> int | None:
    index = _block_at(spans, position)
    block = group[index]
    if block.start_line is None or block.end_line is None:
        return None
    start, end = spans[index]
    if position < start:  # cae en el separador: primera línea del siguiente / última del anterior
        return block.start_line if first else group[index - 1].end_line
    return min(block.start_line + text.count("\n", start, position), block.end_line)


def _windows(text: str, policy: ChunkPolicy) -> Iterator[tuple[int, int]]:
    """Rangos `[inicio, fin)` de cada fragmento, sin espacios en los extremos ni vacíos."""
    length = len(text)
    start = 0
    while start < length:
        end = length if length - start <= policy.size else _cut(text, start, policy.size)
        lo, hi = _trim(text, start, end)
        if lo < hi:
            yield lo, hi
        if end >= length:
            return
        start = _next_start(text, start, end, policy.overlap)


def _cut(text: str, start: int, size: int) -> int:
    """Mejor final para el fragmento que empieza en `start` (nunca antes de la mitad del tamaño)."""
    limit = start + size
    floor = start + size // 2
    for separator in _BREAKS:
        index = text.rfind(separator, floor, limit)
        if index != -1:
            return index + len(separator)
    return limit


def _next_start(text: str, start: int, end: int, overlap: int) -> int:
    """Inicio del siguiente fragmento: `overlap` atrás desde `end`, sin partir una palabra."""
    candidate = end - overlap
    if overlap and 0 < candidate < end and not text[candidate - 1].isspace():
        gap = next((i for i in range(candidate, end) if text[i].isspace()), None)
        if gap is not None:
            candidate = gap + 1
    return candidate if candidate > start else end


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
