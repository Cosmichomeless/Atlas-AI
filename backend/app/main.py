from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.request_id import HEADER as REQUEST_ID_HEADER
from app.api.request_id import RequestIdMiddleware
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.features.health.router import HealthResponse
from app.features.health.router import health as liveness


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Atlas AI",
        version="0.1.0",
        description=(
            "Pregunta a tus documentos y obtén respuestas con citas.\n\n"
            "Todos los endpoints de negocio viven bajo `/api/v1`. Los errores comparten el esquema "
            '`ErrorResponse` (`{"error": {"code", "message", "request_id", "details"}}`).'
        ),
    )
    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)

    # Sonda de liveness para orquestadores, fuera del contrato versionado.
    @app.get("/health", include_in_schema=False)
    def root_health() -> HealthResponse:
        return liveness()

    return app


app = create_app()
