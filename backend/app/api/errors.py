"""Formato de error común de la API y su registro en FastAPI.

Toda respuesta de error (HTTP 4xx/5xx) tiene la forma::

    {"error": {"code": "not_found", "message": "...", "request_id": "...", "details": null}}

`code` es estable y apto para lógica de cliente; `message` es legible por personas.
"""

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# status HTTP -> (code estable, mensaje por defecto)
STATUS_ERRORS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Solicitud incorrecta."),
    401: ("unauthorized", "Autenticación requerida."),
    403: ("forbidden", "No tienes permiso para realizar esta acción."),
    404: ("not_found", "Recurso no encontrado."),
    405: ("method_not_allowed", "Método no permitido."),
    409: ("conflict", "La solicitud entra en conflicto con el estado actual del recurso."),
    413: ("payload_too_large", "El contenido enviado es demasiado grande."),
    415: ("unsupported_media_type", "Tipo de contenido no admitido."),
    422: ("validation_error", "Los datos enviados no son válidos."),
    429: ("too_many_requests", "Demasiadas solicitudes. Inténtalo de nuevo más tarde."),
    500: ("internal_error", "Error interno del servidor."),
    503: ("service_unavailable", "Servicio no disponible."),
}


class ErrorDetail(BaseModel):
    """Un problema concreto, normalmente asociado a un campo de la solicitud."""

    loc: list[str | int] = Field(description="Ubicación del problema, p. ej. ['body', 'email'].")
    message: str
    type: str = Field(description="Tipo de error de validación.")


class ErrorBody(BaseModel):
    code: str = Field(description="Identificador estable del error, p. ej. 'not_found'.")
    message: str = Field(description="Descripción legible del error.")
    request_id: str | None = Field(
        default=None, description="Identificador de la solicitud (cabecera X-Request-ID)."
    )
    details: list[ErrorDetail] | None = Field(
        default=None, description="Detalle por campo; presente en errores de validación."
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Error de negocio con status HTTP y code estable."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Declara en OpenAPI las respuestas de error con el esquema común."""
    return {
        status: {
            "model": ErrorResponse,
            "description": STATUS_ERRORS.get(status, ("error", "Error"))[1],
        }
        for status in status_codes
    }


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, request_id=_request_id(request), details=details
        )
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=headers)


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(AppError, exc)
    return _response(request, err.status_code, err.code, err.message)


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(StarletteHTTPException, exc)
    code, default_message = STATUS_ERRORS.get(err.status_code, ("error", "Error."))
    message = err.detail if isinstance(err.detail, str) and err.detail else default_message
    # Los textos genéricos de Starlette ("Not Found") se sustituyen por el mensaje por defecto.
    if message in {"Not Found", "Method Not Allowed", "Forbidden", "Unauthorized"}:
        message = default_message
    return _response(
        request, err.status_code, code, message, headers=dict(err.headers) if err.headers else None
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    details = [
        ErrorDetail(loc=[*e["loc"]], message=str(e["msg"]), type=str(e["type"]))
        for e in cast(RequestValidationError, exc).errors()
    ]
    code, message = STATUS_ERRORS[422]
    return _response(request, 422, code, message, details=details)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Error no controlado (request_id=%s)", _request_id(request), exc_info=exc)
    code, message = STATUS_ERRORS[500]
    return _response(request, 500, code, message)  # nunca se expone el detalle interno


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
