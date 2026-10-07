import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import AppError, error_responses
from app.core.config import get_settings
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.documents import queries
from app.features.documents.deletion import DocumentBusyError, delete_owned
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import FileStorage, StoredFileTooLarge, get_storage
from app.features.documents.uploads import file_too_large, validate_upload

router = APIRouter(prefix="/documents", tags=["documents"], responses=error_responses(401))

MAX_PAGE_SIZE = 100
# Margen para las cabeceras multipart al comparar con `Content-Length`.
MULTIPART_OVERHEAD = 64 * 1024


def upload_limit_bytes() -> int:
    """Tamaño máximo de un archivo (`MAX_UPLOAD_MB`); dependencia sustituible en las pruebas."""
    return get_settings().max_upload_mb * 1024 * 1024


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    """Resumen más el progreso de ingestión; nunca expone propietario ni datos del worker."""

    error_summary: str | None = Field(description="Último error de procesamiento, si lo hubo.")
    attempts: int
    processing_started_at: datetime | None
    processed_at: datetime | None


class DocumentPassage(BaseModel):
    """Pasaje original de un documento: lo que una cita señala, con su ubicación."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    ordinal: int = Field(description="Posición del fragmento dentro del documento.")
    text: str
    page: int | None
    section: str | None
    start_line: int | None
    end_line: int | None


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    total: int = Field(description="Documentos que cumplen el filtro, sin paginar.")
    limit: int
    offset: int


@router.get("", summary="Listar mis documentos", responses=error_responses(422))
def list_documents(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[DocumentStatus | None, Query(description="Filtrar por estado.")] = None,
) -> DocumentList:
    """Documentos del usuario autenticado, los más recientes primero, paginados."""
    items, total = queries.list_owned_page(
        session, user.id, limit=limit, offset=offset, status=status
    )
    return DocumentList(
        items=[DocumentSummary.model_validate(d) for d in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{document_id}",
    summary="Ver un documento",
    responses=error_responses(404, 422),
)
def get_document(document_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> DocumentDetail:
    """Detalle con el estado de ingestión. Un ID inexistente o ajeno devuelve el mismo 404."""
    document = queries.get_owned(session, user.id, document_id)
    if document is None:
        raise AppError(404, "document_not_found", "Documento no encontrado.")
    return DocumentDetail.model_validate(document)


@router.get(
    "/{document_id}/chunks/{chunk_id}",
    summary="Ver el pasaje original de una cita",
    responses=error_responses(404, 422),
)
def get_passage(
    document_id: uuid.UUID, chunk_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> DocumentPassage:
    """Texto y ubicación del fragmento que respalda una cita.

    Un documento o fragmento inexistente, borrado, reindexado o ajeno devuelve el mismo 404.
    """
    found = queries.get_owned_chunk(session, user.id, document_id, chunk_id)
    if found is None:
        raise AppError(404, "passage_not_found", "El pasaje ya no está disponible.")
    document, chunk = found
    return DocumentPassage(
        chunk_id=chunk.id,
        document_id=document.id,
        filename=document.filename,
        ordinal=chunk.ordinal,
        text=chunk.text,
        page=chunk.page,
        section=chunk.section,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
    )


@router.post(
    "",
    status_code=201,
    summary="Subir un documento",
    responses=error_responses(413, 415, 422),
)
def upload_document(
    file: UploadFile,
    user: CurrentUser,
    session: SessionDep,
    storage: Annotated[FileStorage, Depends(get_storage)],
    max_bytes: Annotated[int, Depends(upload_limit_bytes)],
    content_length: Annotated[int | None, Header(include_in_schema=False)] = None,
) -> DocumentDetail:
    """Sube un PDF, TXT o Markdown y crea un documento en estado `UPLOADED`.

    Se valida el tipo (por extensión y contenido real), que no esté vacío y que no supere el máximo.
    Un archivo rechazado no deja ni registro ni archivo en el almacenamiento.
    """
    if content_length is not None and content_length > max_bytes + MULTIPART_OVERHEAD:
        raise file_too_large()

    checked = validate_upload(file.file, file.filename, max_bytes=max_bytes)
    document = Document(
        owner_id=user.id,
        filename=checked.filename,
        content_type=checked.content_type,
        size_bytes=checked.size_bytes,
    )
    try:
        written = storage.save(document.storage_key, file.file, max_bytes=max_bytes)
    except StoredFileTooLarge as exc:
        raise file_too_large() from exc
    try:
        document.size_bytes = written
        session.add(document)
        session.commit()
    except Exception:
        session.rollback()
        storage.delete(document.storage_key)
        raise
    return DocumentDetail.model_validate(document)


@router.delete(
    "/{document_id}",
    status_code=204,
    summary="Eliminar un documento",
    responses=error_responses(404, 409, 422),
)
def delete_document(
    document_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: Annotated[FileStorage, Depends(get_storage)],
) -> Response:
    """Elimina el documento con su archivo, fragmentos y vectores.

    Un ID inexistente o ajeno devuelve el mismo 404. Si un worker lo está procesando responde 409:
    se puede reintentar al terminar.
    """
    try:
        deleted = delete_owned(session, storage, user.id, document_id)
    except DocumentBusyError as exc:
        raise AppError(
            409,
            "document_processing",
            "El documento se está procesando; inténtalo de nuevo en unos instantes.",
        ) from exc
    if not deleted:
        raise AppError(404, "document_not_found", "Documento no encontrado.")
    return Response(status_code=204)
