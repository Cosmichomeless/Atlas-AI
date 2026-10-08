from fastapi import APIRouter, Depends

from app.api.csrf import verify_csrf
from app.api.errors import error_responses
from app.features.auth.router import router as auth_router
from app.features.documents.router import router as documents_router
from app.features.health.router import router as health_router
from app.features.questions.router import router as questions_router
from app.features.search.router import router as search_router
from app.features.usage.router import router as usage_router

API_V1_PREFIX = "/api/v1"

# Respuestas de error comunes a todos los endpoints de la versión 1.
# Todas las operaciones mutables de la API exigen defensa CSRF (ver app/api/csrf.py).
api_router = APIRouter(
    prefix=API_V1_PREFIX,
    responses=error_responses(403, 422, 500),
    dependencies=[Depends(verify_csrf)],
)
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(documents_router)
api_router.include_router(search_router)

api_router.include_router(questions_router)
api_router.include_router(usage_router)
