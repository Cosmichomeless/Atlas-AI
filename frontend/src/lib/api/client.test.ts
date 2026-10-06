import { describe, expect, it } from "vitest";

import { createApiClient, unwrap } from "./client";
import { ApiError, isErrorResponse, toApiError } from "./errors";

function json(status: number, body: unknown): typeof fetch {
  return async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
}

describe("cliente tipado", () => {
  it("devuelve los datos tipados de /api/v1/health", async () => {
    const client = createApiClient({ baseUrl: "http://api.test", fetch: json(200, { status: "ok" }) });

    const data = await unwrap(client.GET("/api/v1/health"));

    expect(data.status).toBe("ok");
  });

  it("lanza ApiError con el formato común de errores", async () => {
    const body = {
      error: { code: "database_unavailable", message: "Base de datos no disponible", request_id: "r1", details: null },
    };
    const client = createApiClient({ baseUrl: "http://api.test", fetch: json(503, body) });

    const error = await unwrap(client.GET("/api/v1/health/db")).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 503, code: "database_unavailable", requestId: "r1" });
  });

  it("traduce fallos de red a network_error", async () => {
    const client = createApiClient({
      baseUrl: "http://api.test",
      fetch: async () => {
        throw new TypeError("fetch failed");
      },
    });

    const error = await unwrap(client.GET("/api/v1/health")).catch((e: unknown) => e);

    expect(error).toMatchObject({ status: 0, code: "network_error" });
  });
});

describe("errores", () => {
  it("reconoce el contrato y tolera respuestas que no lo cumplen", () => {
    expect(isErrorResponse({ error: { code: "x", message: "y" } })).toBe(true);
    expect(isErrorResponse({ detail: "Not Found" })).toBe(false);
    expect(toApiError(502, "<html>Bad Gateway</html>")).toMatchObject({
      status: 502,
      code: "unexpected_error",
    });
  });

  it("conserva el detalle por campo de los errores de validación", () => {
    const error = toApiError(422, {
      error: {
        code: "validation_error",
        message: "Los datos enviados no son válidos.",
        request_id: null,
        details: [{ loc: ["body", "email"], message: "invalid", type: "value_error" }],
      },
    });

    expect(error.details?.[0].loc).toEqual(["body", "email"]);
  });
});

describe("protección CSRF", () => {
  function recorder(options: { rejectFirstMutation?: boolean } = {}) {
    const calls: { url: string; method: string; csrf: string | null }[] = [];
    let issued = 0;
    let rejectNext = options.rejectFirstMutation ?? false;
    const respond = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    const fetchFake: typeof fetch = async (input, init) => {
      const request = new Request(input as RequestInfo, init);
      calls.push({ url: request.url, method: request.method, csrf: request.headers.get("X-CSRF-Token") });
      if (request.url.endsWith("/api/v1/auth/csrf")) {
        issued += 1;
        return respond(200, { csrf_token: `token-${issued}` });
      }
      if (request.method === "POST" && rejectNext) {
        rejectNext = false;
        const error = { code: "csrf_failed", message: "Token CSRF no válido.", request_id: "r", details: null };
        return respond(403, { error });
      }
      return respond(200, { id: "1", email: "a@b.co" });
    };
    return { calls, fetchFake };
  }
  const login = { body: { email: "a@b.co", password: "correct horse battery" } };

  it("no pide token en lecturas", async () => {
    const { calls, fetchFake } = recorder();
    const client = createApiClient({ baseUrl: "http://api.test", fetch: fetchFake });

    await client.GET("/api/v1/health");

    expect(calls.map((c) => c.url)).toEqual(["http://api.test/api/v1/health"]);
  });

  it("obtiene el token una vez y lo envía en las mutaciones", async () => {
    const { calls, fetchFake } = recorder();
    const client = createApiClient({ baseUrl: "http://api.test", fetch: fetchFake });

    await client.POST("/api/v1/auth/login", login);
    await client.POST("/api/v1/auth/logout");

    const mutations = calls.filter((c) => c.method === "POST");
    expect(mutations.map((c) => c.csrf)).toEqual(["token-1", "token-1"]);
    expect(calls.filter((c) => c.url.endsWith("/auth/csrf"))).toHaveLength(1);
  });

  it("descarta el token cacheado tras un csrf_failed", async () => {
    const { calls, fetchFake } = recorder({ rejectFirstMutation: true });
    const client = createApiClient({ baseUrl: "http://api.test", fetch: fetchFake });

    await client.POST("/api/v1/auth/login", login);
    await client.POST("/api/v1/auth/login", login);

    const posts = calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => c.csrf)).toEqual(["token-1", "token-2"]);
  });
});
