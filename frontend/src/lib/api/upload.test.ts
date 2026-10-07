import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./errors";
import { uploadDocument } from "./upload";
import type { CsrfStore } from "./csrf";

const DETAIL = { id: "d1", filename: "a.txt", status: "UPLOADED" };

class FakeXhr {
  method = "";
  url = "";
  headers: Record<string, string> = {};
  withCredentials = false;
  responseType = "";
  status = 0;
  responseText = "";
  body: unknown = null;
  aborted = false;
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  progress(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total } as ProgressEvent);
  }
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = typeof body === "string" ? body : JSON.stringify(body);
    this.onload?.();
  }
}

function setup() {
  const requests: FakeXhr[] = [];
  const tokens = ["t1", "t2"];
  const csrf: CsrfStore = {
    token: vi.fn(async () => tokens[0]),
    reset: vi.fn(() => {
      tokens.shift();
    }),
  };
  const createRequest = () => {
    const xhr = new FakeXhr();
    requests.push(xhr);
    return xhr as unknown as XMLHttpRequest;
  };
  return { requests, csrf, createRequest, baseUrl: "http://api.test" };
}

const FILE = new File(["hola"], "a.txt", { type: "text/plain" });
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("uploadDocument", () => {
  it("envía multipart con cookie, token CSRF y notifica el progreso", async () => {
    const env = setup();
    const onProgress = vi.fn();
    const promise = uploadDocument(FILE, { ...env, onProgress });
    await tick();
    const xhr = env.requests[0]!;

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("http://api.test/api/v1/documents");
    expect(xhr.withCredentials).toBe(true);
    expect(xhr.headers["X-CSRF-Token"]).toBe("t1");
    expect((xhr.body as FormData).get("file")).toBeInstanceOf(File);

    xhr.progress(25, 100);
    xhr.progress(50, 100);
    xhr.respond(201, DETAIL);

    await expect(promise).resolves.toEqual(DETAIL);
    expect(onProgress.mock.calls.map(([fraction]) => fraction)).toEqual([0.25, 0.5, 1]);
  });

  it.each([
    [413, "file_too_large", "El archivo supera el máximo."],
    [415, "unsupported_media_type", "Tipo no admitido."],
    [422, "invalid_filename", "Nombre no válido."],
  ])("convierte el error %i en ApiError con el mensaje del servidor", async (status, code, message) => {
    const env = setup();
    const promise = uploadDocument(FILE, env);
    await tick();
    env.requests[0]!.respond(status, { error: { code, message, request_id: null, details: null } });

    await expect(promise).rejects.toMatchObject({ status, code, message });
  });

  it("trata un cuerpo que no es JSON como error inesperado", async () => {
    const env = setup();
    const promise = uploadDocument(FILE, env);
    await tick();
    env.requests[0]!.respond(502, "<html>Bad gateway</html>");

    await expect(promise).rejects.toBeInstanceOf(ApiError);
  });

  it("informa de un fallo de red", async () => {
    const env = setup();
    const promise = uploadDocument(FILE, env);
    await tick();
    env.requests[0]!.onerror?.();

    await expect(promise).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("reintenta una vez con un token nuevo si el CSRF caducó", async () => {
    const env = setup();
    const promise = uploadDocument(FILE, env);
    await tick();
    env.requests[0]!.respond(403, {
      error: { code: "csrf_failed", message: "CSRF", request_id: null, details: null },
    });
    await tick();

    expect(env.csrf.reset).toHaveBeenCalledOnce();
    expect(env.requests).toHaveLength(2);
    expect(env.requests[1]!.headers["X-CSRF-Token"]).toBe("t2");
    env.requests[1]!.respond(201, DETAIL);
    await expect(promise).resolves.toEqual(DETAIL);
  });

  it("no reintenta ante otros 403", async () => {
    const env = setup();
    const promise = uploadDocument(FILE, env);
    await tick();
    env.requests[0]!.respond(403, {
      error: { code: "forbidden", message: "No", request_id: null, details: null },
    });

    await expect(promise).rejects.toMatchObject({ status: 403 });
    expect(env.requests).toHaveLength(1);
  });

  it("se cancela con AbortSignal", async () => {
    const env = setup();
    const controller = new AbortController();
    const promise = uploadDocument(FILE, { ...env, signal: controller.signal });
    await tick();
    controller.abort();

    await expect(promise).rejects.toMatchObject({ code: "aborted" });
    expect(env.requests[0]!.aborted).toBe(true);
  });
});
