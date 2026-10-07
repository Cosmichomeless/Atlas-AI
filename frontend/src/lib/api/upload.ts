import { API_BASE_URL, csrf as defaultCsrf } from "./client";
import { CSRF_HEADER } from "./csrf";
import type { CsrfStore } from "./csrf";
import type { DocumentDetail } from "./documents";
import { ApiError, toApiError } from "./errors";

export interface UploadOptions {
  /** Fracción enviada, de 0 a 1. */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
  baseUrl?: string;
  csrf?: CsrfStore;
  /** Solo para tests: fábrica de `XMLHttpRequest`. */
  createRequest?: () => XMLHttpRequest;
}

function send(file: File, token: string, options: UploadOptions): Promise<DocumentDetail> {
  return new Promise((resolve, reject) => {
    const xhr = (options.createRequest ?? (() => new XMLHttpRequest()))();
    const abort = () => xhr.abort();

    xhr.open("POST", `${options.baseUrl ?? API_BASE_URL}/api/v1/documents`);
    xhr.withCredentials = true;
    xhr.responseType = "text";
    xhr.setRequestHeader(CSRF_HEADER, token);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) options.onProgress?.(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "No se pudo conectar con el servidor."));
    xhr.ontimeout = xhr.onerror;
    xhr.onabort = () => reject(new ApiError(0, "aborted", "Subida cancelada."));
    xhr.onload = () => {
      options.signal?.removeEventListener("abort", abort);
      let payload: unknown = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        // cuerpo no JSON (p. ej. un 502 de un proxy): se trata como respuesta inesperada
      }
      if (xhr.status === 201 && payload) {
        options.onProgress?.(1);
        resolve(payload as DocumentDetail);
      } else {
        reject(toApiError(xhr.status, payload));
      }
    };

    if (options.signal) {
      if (options.signal.aborted) return reject(new ApiError(0, "aborted", "Subida cancelada."));
      options.signal.addEventListener("abort", abort, { once: true });
    }
    const body = new FormData();
    body.append("file", file, file.name);
    xhr.send(body);
  });
}

/**
 * Sube un documento con `XMLHttpRequest` porque `fetch` no informa del progreso de envío.
 * Reintenta una vez si el servidor rechaza un token CSRF caducado.
 */
export async function uploadDocument(file: File, options: UploadOptions = {}): Promise<DocumentDetail> {
  const store = options.csrf ?? defaultCsrf;
  try {
    return await send(file, await store.token(), options);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 403 || error.code !== "csrf_failed") throw error;
    store.reset();
    return send(file, await store.token(), options);
  }
}
