import { vi } from "vitest";

export interface Call {
  method: string;
  path: string;
  body: unknown;
}

export type Handler = (call: Call, request: Request) => Response | Promise<Response>;

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function errorResponse(status: number, code: string, message: string, details: unknown = null) {
  return jsonResponse(status, { error: { code, message, request_id: null, details } });
}

export const NO_CONTENT = () => new Response(null, { status: 204 });

/**
 * Sustituye `fetch` global por un servidor simulado: `routes` se indexa por "MÉTODO /ruta" y recibe
 * la llamada ya decodificada. Una ruta no declarada hace fallar el test (no hay red real).
 */
export function mockApi(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input as RequestInfo, init);
    const path = new URL(request.url).pathname;
    const method = request.method.toUpperCase();
    if (path === "/api/v1/auth/csrf") return jsonResponse(200, { csrf_token: "test-token" });
    const text = request.headers.get("content-type")?.includes("json") ? await request.clone().text() : "";
    const call: Call = { method, path, body: text ? JSON.parse(text) : null };
    calls.push(call);
    const handler = routes[`${method} ${path}`];
    if (!handler) throw new Error(`Ruta sin simular: ${method} ${path}`);
    return handler(call, request);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export const USER = { id: "11111111-1111-4111-8111-111111111111", email: "ana@example.com", created_at: "2026-01-01T00:00:00Z" };
