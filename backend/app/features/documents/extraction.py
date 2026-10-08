"""Extracción de texto con procedencia: cada bloque sabe de qué documento y de dónde sale.

Un PDF produce un bloque por página con texto (`page`, empezando en 1). Un archivo de texto produce
un bloque por párrafo y uno Markdown, además, por sección (`section`, ruta de encabezados). Los
archivos de texto también registran el rango de líneas (`start_line`/`end_line`, desde 1).

El análisis tiene límites duros de tamaño, páginas, caracteres y tiempo (`EXTRACTION_*`) y, en el
worker, corre en un subproceso que se mata al agotar el plazo (ver `isolation.py`). Superar un
límite o fallar el análisis es un `ExtractionError` con una causa comprensible para el usuario; la
función `extract_or_fail` la guarda en el documento y lo deja en FAILED.
"""

import logging
import uuid
from dataclasses import asdict, dataclass
from typing import BinaryIO

from app.core.config import Settings
from app.features.documents.isolation import Isolation, run_isolated
from app.features.documents.models import Document
from app.features.documents.parsing import Limits, ParsedBlock, ParseError, parse
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import FileStorage, StorageError

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """El documento no se puede convertir en texto; el mensaje está pensado para el usuario."""


@dataclass(frozen=True)
class ExtractionLimits:
    """Topes del análisis de un archivo; el worker real los toma de `Settings` y aísla."""

    max_bytes: int = 20 * 1024 * 1024
    max_pages: int = 500
    max_chars: int = 5_000_000
    timeout_seconds: int = 60
    isolated: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExtractionLimits":
        return cls(
            max_bytes=settings.max_upload_mb * 1024 * 1024,
            max_pages=settings.extraction_max_pages,
            max_chars=settings.extraction_max_chars,
            timeout_seconds=settings.extraction_timeout_seconds,
            isolated=settings.extraction_isolated,
        )


DEFAULT_LIMITS = ExtractionLimits()


@dataclass(frozen=True)
class ExtractedBlock:
    """Fragmento de texto y su localización de origen."""

    document_id: uuid.UUID
    text: str
    page: int | None = None
    section: str | None = None
    start_line: int | None = None
    end_line: int | None = None


def extract_or_fail(
    document: Document, storage: FileStorage, limits: ExtractionLimits = DEFAULT_LIMITS
) -> list[ExtractedBlock]:
    """Extrae el texto de un documento en PROCESSING; si no puede, lo pasa a FAILED y devuelve [].

    El llamador (el worker) es quien confirma la transacción.
    """
    try:
        with storage.open(document.storage_key) as file:
            return extract_blocks(document.id, file, document.content_type, limits)
    except ExtractionError as exc:
        document.transition_to(DocumentStatus.FAILED, error_summary=str(exc))
    except StorageError:
        logger.exception("Archivo no disponible para el documento %s", document.id)
        document.transition_to(
            DocumentStatus.FAILED, error_summary="No se encontró el archivo original."
        )
    return []


def extract_blocks(
    document_id: uuid.UUID,
    file: BinaryIO,
    content_type: str,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> list[ExtractedBlock]:
    """Lee el archivo (con tope de tamaño) y lo analiza, en un subproceso con plazo si se pide."""
    data = file.read(limits.max_bytes + 1)
    if len(data) > limits.max_bytes:
        megabytes = limits.max_bytes // (1024 * 1024)
        raise ExtractionError(f"El archivo supera el tamaño máximo permitido ({megabytes} MB).")
    parse_limits = Limits(limits.max_pages, limits.max_chars)
    try:
        if limits.isolated:
            parsed = run_isolated(
                content_type, data, parse_limits, Isolation(limits.timeout_seconds)
            )
        else:
            parsed = _parse_in_process(content_type, data, parse_limits)
    except ParseError as exc:
        raise ExtractionError(str(exc)) from exc
    return [ExtractedBlock(document_id, **asdict(block)) for block in parsed]


def _parse_in_process(content_type: str, data: bytes, limits: Limits) -> list[ParsedBlock]:
    try:
        return parse(content_type, data, limits)
    except ParseError as exc:
        if exc.__cause__ is not None:
            logger.warning("Archivo ilegible: %s", exc.__cause__)
        raise
