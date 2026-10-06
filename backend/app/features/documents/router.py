import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import AppError, error_responses
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.documents import queries
from app.features.documents.states import DocumentStatus

router = APIRouter(prefix="/documents", tags=["documents"], responses=error_responses(401))

MAX_PAGE_SIZE = 100


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
