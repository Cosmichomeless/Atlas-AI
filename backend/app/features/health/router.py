from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import AppError, error_responses
from app.core.db import get_engine

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


class DatabaseHealthResponse(BaseModel):
    status: str
    pgvector_version: str


@router.get("/health", summary="Liveness probe")
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/health/db",
    summary="Readiness probe de base de datos",
    description="Comprueba la conexión a PostgreSQL y que la extensión pgvector está habilitada.",
    responses=error_responses(503),
)
def health_db() -> DatabaseHealthResponse:
    try:
        with get_engine().connect() as conn:
            version = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise AppError(503, "database_unavailable", "Base de datos no disponible") from exc
    if version is None:
        raise AppError(503, "pgvector_missing", "La extensión pgvector no está habilitada")
    return DatabaseHealthResponse(status="ok", pgvector_version=version)
