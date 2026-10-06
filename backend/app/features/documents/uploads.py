"""Validación de archivos subidos: tipo, contenido, tamaño y nombre.

No se confía en nada que declare el cliente: el tipo se deduce de la extensión y se comprueba contra
el contenido real (cabecera `%PDF-` o texto UTF-8 sin bytes nulos), y el `Content-Type` enviado se
ignora. El nombre original solo se conserva como metadato, ya saneado.
"""

import codecs
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import BinaryIO

from app.api.errors import AppError

MAX_FILENAME_LENGTH = 255
PDF_MAGIC = b"%PDF-"
TEXT_CHUNK = 64 * 1024


class FileKind(StrEnum):
    PDF = "application/pdf"
    TEXT = "text/plain"
    MARKDOWN = "text/markdown"


EXTENSIONS: dict[str, FileKind] = {
    ".pdf": FileKind.PDF,
    ".txt": FileKind.TEXT,
    ".md": FileKind.MARKDOWN,
    ".markdown": FileKind.MARKDOWN,
}
ACCEPTED = "PDF, TXT o Markdown (.pdf, .txt, .md, .markdown)"


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    content_type: str
    size_bytes: int


def file_too_large() -> AppError:
    return AppError(413, "file_too_large", "El archivo supera el tamaño máximo permitido.")


def sanitize_filename(raw: str | None) -> str:
    """Nombre seguro para mostrar: sin directorios, sin caracteres de control, longitud acotada."""
    name = (raw or "").replace("\\", "/")
    name = PurePosixPath(name).name
    name = unicodedata.normalize("NFC", name)
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip().lstrip(".")
    if not name:
        raise AppError(422, "invalid_filename", "El archivo debe tener un nombre válido.")
    if len(name) > MAX_FILENAME_LENGTH:
        raise AppError(
            422, "invalid_filename", f"El nombre supera los {MAX_FILENAME_LENGTH} caracteres."
        )
    return name


def measure(file: BinaryIO) -> int:
    """Tamaño real del contenido recibido (no el que declare la cabecera)."""
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    return size


def validate_upload(file: BinaryIO, raw_filename: str | None, *, max_bytes: int) -> ValidatedUpload:
    """Valida el archivo recibido y lo deja listo para leerse desde el principio."""
    filename = sanitize_filename(raw_filename)
    kind = EXTENSIONS.get(PurePosixPath(filename).suffix.lower())
    if kind is None:
        raise AppError(415, "unsupported_media_type", f"Solo se admiten archivos {ACCEPTED}.")

    size = measure(file)
    if size == 0:
        raise AppError(422, "empty_file", "El archivo está vacío.")
    if size > max_bytes:
        raise file_too_large()

    if not _content_matches(file, kind):
        raise AppError(
            415,
            "unsupported_media_type",
            f"El contenido no corresponde a un archivo {kind.name.lower()} válido.",
        )
    file.seek(0)
    return ValidatedUpload(filename=filename, content_type=kind.value, size_bytes=size)


def _content_matches(file: BinaryIO, kind: FileKind) -> bool:
    file.seek(0)
    if kind is FileKind.PDF:
        return file.read(len(PDF_MAGIC)) == PDF_MAGIC
    return _is_utf8_text(file)


def _is_utf8_text(file: BinaryIO) -> bool:
    """UTF-8 válido y sin bytes nulos, leído por bloques para no cargarlo entero en memoria."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while chunk := file.read(TEXT_CHUNK):
            if b"\x00" in chunk:
                return False
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True
