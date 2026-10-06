import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.api.errors import error_responses
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.documents import queries

router = APIRouter(prefix="/documents", tags=["documents"], responses=error_responses(401))


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentSummary]


@router.get("", summary="Listar mis documentos")
def list_documents(user: CurrentUser, session: SessionDep) -> DocumentList:
    """Solo devuelve documentos del usuario autenticado. Paginación y estados llegan en #13."""
    return DocumentList(
        items=[DocumentSummary.model_validate(d) for d in queries.list_owned(session, user.id)]
    )
