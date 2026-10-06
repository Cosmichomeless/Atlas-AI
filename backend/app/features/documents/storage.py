"""Almacenamiento privado de archivos: PostgreSQL guarda referencias (claves), nunca binarios.

Los archivos viven fuera de la base de datos, en un directorio que no se sirve públicamente. Cada
archivo se identifica por una clave opaca generada por la aplicación (`<propietario>/<documento>/
<nombre>`); los nombres que escribe el usuario no forman parte de ninguna ruta. Aun así, toda clave
se valida y toda ruta se resuelve y se comprueba contra la raíz, de modo que ni una clave malformada
ni un enlace simbólico puedan sacar una lectura o escritura del espacio configurado.
"""

import contextlib
import os
import re
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import get_settings

ORIGINAL = "original"
MAX_KEY_LENGTH = 512
CHUNK_SIZE = 1024 * 1024
DIR_MODE = 0o700
FILE_MODE = 0o600

# Segmentos de una clave: letras, dígitos, guion, guion bajo y punto, sin empezar por punto
# (así no existen `.`, `..` ni archivos ocultos).
_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


class StorageError(Exception):
    """Fallo del almacenamiento de archivos."""


class InvalidStorageKey(StorageError):
    """La clave no es una referencia válida dentro del espacio configurado."""


class StoredFileNotFound(StorageError):
    """No existe ningún archivo para esa clave."""


class StoredFileTooLarge(StorageError):
    """El contenido supera el tamaño máximo permitido."""


def document_key(owner_id: uuid.UUID, document_id: uuid.UUID, name: str = ORIGINAL) -> str:
    """Clave de un archivo de un documento: el original o un derivado (`name`)."""
    return validate_key(f"{owner_id}/{document_id}/{name}")


def validate_key(key: str) -> str:
    """Devuelve la clave si es segura; lanza `InvalidStorageKey` si no lo es."""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise InvalidStorageKey("Clave de almacenamiento vacía o demasiado larga.")
    if not all(_SEGMENT.fullmatch(segment) for segment in key.split("/")):
        raise InvalidStorageKey("Clave de almacenamiento no válida.")
    return key


class FileStorage(Protocol):
    """Frontera de almacenamiento: permite cambiar el disco local por otro backend."""

    def save(self, key: str, data: BinaryIO, *, max_bytes: int | None = None) -> int: ...

    def open(self, key: str) -> BinaryIO: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class LocalFileStorage:
    """Archivos en un directorio local, con permisos restringidos al usuario del proceso."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise InvalidStorageKey("La clave apunta fuera del espacio de almacenamiento.")
        return path

    def save(self, key: str, data: BinaryIO, *, max_bytes: int | None = None) -> int:
        """Guarda el contenido de forma atómica y devuelve los bytes escritos.

        Se escribe en un temporal del mismo directorio y se renombra al final: nunca queda un
        archivo a medias bajo la clave definitiva. Si supera `max_bytes`, se descarta.
        """
        path = self._path(key)
        self._make_dirs(path.parent)
        if not path.parent.resolve().is_relative_to(self.root):
            raise InvalidStorageKey("La clave apunta fuera del espacio de almacenamiento.")

        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := data.read(CHUNK_SIZE):
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise StoredFileTooLarge(f"El archivo supera los {max_bytes} bytes.")
                    out.write(chunk)
            os.chmod(tmp_name, FILE_MODE)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        return written

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        try:
            return path.open("rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise StoredFileNotFound(key) from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        """Borra el archivo; no falla si ya no existe."""
        with contextlib.suppress(FileNotFoundError):
            self._path(key).unlink()

    def _make_dirs(self, directory: Path) -> None:
        # `mkdir(mode=)` solo aplica el modo al último nivel y lo filtra el umask: se crean los
        # niveles uno a uno y se fija el modo explícitamente.
        missing = [d for d in (directory, *directory.parents) if not d.exists()]
        for d in reversed(missing):
            d.mkdir(mode=DIR_MODE, exist_ok=True)
            d.chmod(DIR_MODE)


@lru_cache
def get_storage() -> FileStorage:
    """Almacenamiento configurado (`STORAGE_DIR`); dependencia sustituible en las pruebas."""
    return LocalFileStorage(get_settings().storage_dir)
