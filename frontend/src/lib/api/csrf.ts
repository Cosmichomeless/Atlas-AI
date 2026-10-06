import type { Middleware } from "openapi-fetch";

import { isErrorResponse } from "./errors";

export const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * Middleware CSRF: antes de cada operación mutable obtiene (y cachea en memoria) el token de
 * `GET /api/v1/auth/csrf` y lo envía en `X-CSRF-Token`. La cookie asociada es HttpOnly y viaja sola
 * por `credentials: "include"`; el token nunca se guarda en `localStorage`.
 */
export function createCsrfMiddleware(options: { baseUrl: string; fetch?: typeof fetch }): Middleware {
  let cached: Promise<string> | null = null;

  const load = (): Promise<string> => {
    const doFetch = options.fetch ?? globalThis.fetch;
    const pending = doFetch(`${options.baseUrl}/api/v1/auth/csrf`, { credentials: "include" })
      .then(async (response) => {
        const body = (await response.json()) as { csrf_token?: string };
        if (!response.ok || !body.csrf_token) throw new Error("No se pudo obtener el token CSRF.");
        return body.csrf_token;
      })
      .catch((error: unknown) => {
        cached = null; // un fallo no se cachea: el siguiente intento vuelve a pedirlo
        throw error;
      });
    return pending;
  };

  return {
    async onRequest({ request }) {
      if (SAFE_METHODS.has(request.method.toUpperCase())) return request;
      cached ??= load();
      request.headers.set(CSRF_HEADER, await cached);
      return request;
    },
    async onResponse({ response }) {
      if (response.status === 403) {
        const body: unknown = await response.clone().json().catch(() => null);
        if (isErrorResponse(body) && body.error.code === "csrf_failed") cached = null;
      }
      return response;
    },
  };
}
