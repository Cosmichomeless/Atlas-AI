import createClient from "openapi-fetch";

import { createCsrfMiddleware } from "./csrf";
import { ApiError, toApiError } from "./errors";
import type { paths } from "./schema";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Cliente tipado generado a partir del contrato OpenAPI del backend (`npm run api:types`).
 * Las rutas, parámetros y respuestas se validan en compilación; no hay lógica de negocio aquí.
 */
export function createApiClient(options: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  const baseUrl = options.baseUrl ?? API_BASE_URL;
  // `fetch` se resuelve en cada llamada (no al crear el cliente) para poder sustituirlo en tests.
  const fetchImpl = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
  const client = createClient<paths>({ baseUrl, credentials: "include", fetch: fetchImpl });
  client.use(createCsrfMiddleware({ baseUrl, fetch: fetchImpl }));
  return client;
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

/** Como `unwrap` para respuestas sin cuerpo (204): lanza `ApiError` si la petición falla. */
export async function unwrapEmpty(request: Promise<ApiResult<unknown>>): Promise<void> {
  let result: ApiResult<unknown>;
  try {
    result = await request;
  } catch {
    throw new ApiError(0, "network_error", "No se pudo conectar con el servidor.");
  }
  if (result.error !== undefined) throw toApiError(result.response.status, result.error);
}
