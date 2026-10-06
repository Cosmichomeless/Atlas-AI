import createClient from "openapi-fetch";

import { ApiError, toApiError } from "./errors";
import type { paths } from "./schema";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Cliente tipado generado a partir del contrato OpenAPI del backend (`npm run api:types`).
 * Las rutas, parámetros y respuestas se validan en compilación; no hay lógica de negocio aquí.
 */
export function createApiClient(options: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  return createClient<paths>({
    baseUrl: options.baseUrl ?? API_BASE_URL,
    credentials: "include",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
}

export const api = createApiClient();

type ApiResult<T> = { data?: T; error?: unknown; response: Response };

/** Devuelve `data` o lanza `ApiError` (también ante fallos de red, con `status` 0). */
export async function unwrap<T>(request: Promise<ApiResult<T>>): Promise<T> {
  let result: ApiResult<T>;
  try {
    result = await request;
  } catch {
    throw new ApiError(0, "network_error", "No se pudo conectar con el servidor.");
  }
  if (result.error !== undefined || result.data === undefined) {
    throw toApiError(result.response.status, result.error);
  }
  return result.data;
}
