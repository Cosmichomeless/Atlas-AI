import type { Middleware } from "openapi-fetch";

import { isErrorResponse } from "./errors";

export const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export interface CsrfStore {
  /** Token vigente (se pide una vez y se cachea en memoria). */
  token(): Promise<string>;
  /** Descarta el token cacheado: el siguiente `token()` pide uno nuevo. */
  reset(): void;
}

/**
 * Fuente del token CSRF: lo obtiene de `GET /api/v1/auth/csrf` y lo cachea en memoria. La cookie
 * asociada es HttpOnly y viaja sola por `credentials: "include"`; el token nunca se guarda en
 * `localStorage`.
 */
export function createCsrfStore(options: { baseUrl: string; fetch?: typeof fetch }): CsrfStore {
  let cached: Promise<string> | null = null;

  const load = (): Promise<string> => {
    const doFetch = options.fetch ?? globalThis.fetch;
    return doFetch(`${options.baseUrl}/api/v1/auth/csrf`, { credentials: "include" })
      .then(async (response) => {
        const body = (await response.json()) as { csrf_token?: string };
        if (!response.ok || !body.csrf_token) throw new Error("No se pudo obtener el token CSRF.");
        return body.csrf_token;
      })
      .catch((error: unknown) => {
        cached = null; // un fallo no se cachea: el siguiente intento vuelve a pedirlo
        throw error;
      });
  };

  return {
    token() {
      cached ??= load();
      return cached;
    },
    reset() {
      cached = null;
    },
  };
}

/** Middleware CSRF: antes de cada operación mutable envía el token en `X-CSRF-Token`. */
export function createCsrfMiddleware(store: CsrfStore): Middleware {
  return {
    async onRequest({ request }) {
      if (SAFE_METHODS.has(request.method.toUpperCase())) return request;
      request.headers.set(CSRF_HEADER, await store.token());
      return request;
    },
    async onResponse({ response }) {
      if (response.status === 403) {
        const body: unknown = await response.clone().json().catch(() => null);
        if (isErrorResponse(body) && body.error.code === "csrf_failed") store.reset();
      }
      return response;
    },
  };
}
