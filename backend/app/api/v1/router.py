from fastapi import APIRouter

from app.api.errors import error_responses
from app.features.health.router import router as health_router

API_V1_PREFIX = "/api/v1"

# Respuestas de error comunes a todos los endpoints de la versión 1.
api_router = APIRouter(prefix=API_V1_PREFIX, responses=error_responses(422, 500))
api_router.include_router(health_router)
