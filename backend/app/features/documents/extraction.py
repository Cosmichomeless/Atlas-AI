"""Extracción de texto con procedencia: cada bloque sabe de qué documento y de dónde sale.

Un PDF produce un bloque por página con texto (`page`, empezando en 1). Un archivo de texto produce
un bloque por párrafo y uno Markdown, además, por sección (`section`, ruta de encabezados). Los
archivos de texto también registran el rango de líneas (`start_line`/`end_line`, desde 1).

Si no hay nada que extraer, `ExtractionError` lleva una causa comprensible para el usuario; la
función `extract_or_fail` la guarda en el documento y lo deja en FAILED.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import FileStorage, StorageError
from app.features.documents.uploads import FileKind

logger = logging.getLogger(__name__)

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


class ExtractionError(Exception):
    """El documento no se puede convertir en texto; el mensaje está pensado para el usuario."""


@dataclass(frozen=True)
class ExtractedBlock:
    """Fragmento de texto y su localización de origen."""

    document_id: uuid.UUID
    text: str
    page: int | None = None
    section: str | None = None
    start_line: int | None = None
    end_line: int | None = None


def extract_or_fail(document: Document, storage: FileStorage) -> list[ExtractedBlock]:
    """Extrae el texto de un documento en PROCESSING; si no puede, lo pasa a FAILED y devuelve [].

    El llamador (el worker) es quien confirma la transacción.
    """
    try:
        with storage.open(document.storage_key) as file:
            return extract_blocks(document.id, file, document.content_type)
    except ExtractionError as exc:
        document.transition_to(DocumentStatus.FAILED, error_summary=str(exc))
    except StorageError:
        logger.exception("Archivo no disponible para el documento %s", document.id)
        document.transition_to(
            DocumentStatus.FAILED, error_summary="No se encontró el archivo original."
        )
    return []


def extract_blocks(
    document_id: uuid.UUID, file: BinaryIO, content_type: str
) -> list[ExtractedBlock]:
    match content_type:
        case FileKind.PDF:
            blocks = _extract_pdf(document_id, file)
        case FileKind.MARKDOWN:
            blocks = _extract_text(document_id, file, markdown=True)
        case FileKind.TEXT:
            blocks = _extract_text(document_id, file, markdown=False)
        case _:
            raise ExtractionError("Tipo de archivo no compatible con la extracción de texto.")
    if not blocks:
        raise ExtractionError("El archivo no contiene texto.")
    return blocks


def _extract_pdf(document_id: uuid.UUID, file: BinaryIO) -> list[ExtractedBlock]:
    try:
        reader = PdfReader(file)
        if reader.is_encrypted:
            raise ExtractionError("El PDF está protegido con contraseña y no se puede leer.")
        blocks = []
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                blocks.append(ExtractedBlock(document_id, text, page=number))
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError) as exc:
        logger.warning("PDF ilegible en el documento %s: %s", document_id, exc)
        raise ExtractionError("El PDF está dañado o no se puede leer.") from exc
    if not blocks:
        raise ExtractionError(
            "El PDF no contiene texto extraíble: parece un escaneo o solo tiene imágenes."
        )
    return blocks


def _extract_text(
    document_id: uuid.UUID, file: BinaryIO, *, markdown: bool
) -> list[ExtractedBlock]:
    try:
        content = file.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("El archivo no está codificado en UTF-8.") from exc

    blocks: list[ExtractedBlock] = []
    headings: list[tuple[int, str]] = []
    paragraph: list[str] = []
    start = 0
    in_fence = False

    def flush(end_line: int) -> None:
        text = "\n".join(paragraph).strip()
        if text:
            section = " > ".join(title for _, title in headings) or None
            blocks.append(ExtractedBlock(document_id, text, None, section, start, end_line))
        paragraph.clear()

    for number, line in enumerate(content.splitlines(), start=1):
        if markdown and _FENCE.match(line):
            in_fence = not in_fence
        heading = None if (not markdown or in_fence) else _HEADING.match(line)
        if heading:
            flush(number - 1)
            level, title = len(heading.group(1)), heading.group(2).strip()
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            start = number
            paragraph.append(line)
        elif not line.strip() and not in_fence:
            # Un encabezado suelto no es un bloque útil: se une al párrafo que le sigue
            if not (markdown and len(paragraph) == 1 and _HEADING.match(paragraph[0])):
                flush(number - 1)
        else:
            if not paragraph:
                start = number
            paragraph.append(line)
    flush(len(content.splitlines()))
    return blocks
