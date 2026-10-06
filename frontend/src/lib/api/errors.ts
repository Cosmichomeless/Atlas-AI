import type { components } from "./schema";

export type ErrorResponse = components["schemas"]["ErrorResponse"];
export type ErrorDetail = components["schemas"]["ErrorDetail"];

/** Error de la API con el formato común `{ error: { code, message, request_id, details } }`. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null = null,
    readonly details: ErrorDetail[] | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = (value as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

/** Convierte el cuerpo de una respuesta de error en un `ApiError`, aunque no siga el contrato. */
export function toApiError(status: number, payload: unknown): ApiError {
  if (isErrorResponse(payload)) {
    const { code, message, request_id, details } = payload.error;
    return new ApiError(status, code, message, request_id ?? null, details ?? null);
  }
  return new ApiError(status, "unexpected_error", "Respuesta inesperada del servidor.");
}
