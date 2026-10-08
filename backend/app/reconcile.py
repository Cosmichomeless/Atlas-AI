"""Comprueba que la base de datos y los archivos subidos cuentan la misma historia.

La base guarda la clave de cada archivo y el archivo vive en `STORAGE_DIR`; tras restaurar una copia
(o ante un borrado manual) pueden desajustarse. Este comando solo **lee** y lo cuenta:

    uv run python -m app.reconcile            # usa DATABASE_URL y STORAGE_DIR
    docker compose exec api python -m app.reconcile

- *Original ausente*: el documento existe en la base pero su archivo no. Sus fragmentos y vectores
  siguen en la base, así que se puede seguir preguntando y citando, pero no se podrá reprocesar.
- *Archivo huérfano*: hay un archivo sin documento. Es seguro borrarlo a mano.

Código de salida 0 si todo cuadra, 1 si hay diferencias, 2 si no se pudo comprobar.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.features.documents.models import Document
from app.features.documents.storage import LocalFileStorage

TEMP_PREFIX = ".upload-"


@dataclass(frozen=True)
class MissingOriginal:
    document_id: str
    filename: str
    status: str
    storage_key: str


@dataclass
class Report:
    documents: int = 0
    files: int = 0
    missing_originals: list[MissingOriginal] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.missing_originals and not self.orphan_files


def stored_keys(root: Path) -> set[str]:
    """Claves (rutas con `/`) de los archivos bajo `root`, sin los temporales de subida."""
    if not root.is_dir():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith(TEMP_PREFIX)
    }


def reconcile(session: Session, storage: LocalFileStorage) -> Report:
    documents = session.execute(
        select(Document.id, Document.filename, Document.status, Document.storage_key)
    ).all()
    on_disk = stored_keys(storage.root)
    report = Report(documents=len(documents), files=len(on_disk))
    referenced = set()
    for document_id, filename, status, key in documents:
        referenced.add(key)
        if key not in on_disk:
            report.missing_originals.append(
                MissingOriginal(str(document_id), filename, status.value, key)
            )
    report.orphan_files = sorted(on_disk - referenced)
    return report


def render(report: Report) -> str:
    lines = [f"Documentos en la base: {report.documents} · archivos en disco: {report.files}"]
    if report.consistent:
        lines.append("Todo cuadra.")
        return "\n".join(lines)
    if report.missing_originals:
        lines.append(f"\nOriginal ausente ({len(report.missing_originals)}):")
        lines += [
            f"  {m.document_id}  {m.status:<10} {m.filename}" for m in report.missing_originals
        ]
    if report.orphan_files:
        lines.append(f"\nArchivo huérfano ({len(report.orphan_files)}):")
        lines += [f"  {key}" for key in report.orphan_files]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="python -m app.reconcile", description=__doc__.split("\n\n")[0]
    ).parse_args(argv)
    storage = LocalFileStorage(get_settings().storage_dir)
    try:
        with get_sessionmaker()() as session:
            report = reconcile(session, storage)
    except Exception as error:  # noqa: BLE001 - se informa y se sale con 2, no con un traceback
        print(f"No se pudo comprobar: {error}", file=sys.stderr)
        return 2
    print(render(report))
    return 0 if report.consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
