import type { components } from "./schema";
import { api, unwrap, unwrapEmpty } from "./client";

export type DocumentSummary = components["schemas"]["DocumentSummary"];
export type DocumentDetail = components["schemas"]["DocumentDetail"];
export type DocumentList = components["schemas"]["DocumentList"];
export type DocumentStatus = components["schemas"]["DocumentStatus"];

export const PAGE_SIZE = 20;

export function listDocuments(offset = 0, limit = PAGE_SIZE): Promise<DocumentList> {
  return unwrap(api.GET("/api/v1/documents", { params: { query: { limit, offset } } }));
}

export function getDocument(id: string): Promise<DocumentDetail> {
  return unwrap(api.GET("/api/v1/documents/{document_id}", { params: { path: { document_id: id } } }));
}

export function deleteDocument(id: string): Promise<void> {
  return unwrapEmpty(
    api.DELETE("/api/v1/documents/{document_id}", { params: { path: { document_id: id } } }),
  );
}

/** Documentos ya procesados (los únicos que pueden responder preguntas), hasta el máximo por página. */
export function listReadyDocuments(): Promise<DocumentList> {
  return unwrap(api.GET("/api/v1/documents", { params: { query: { limit: 100, offset: 0, status: "READY" } } }));
}

export type DocumentPassage = components["schemas"]["DocumentPassage"];

/** Texto original y ubicación del fragmento que respalda una cita. */
export function getPassage(documentId: string, chunkId: string): Promise<DocumentPassage> {
  return unwrap(
    api.GET("/api/v1/documents/{document_id}/chunks/{chunk_id}", {
      params: { path: { document_id: documentId, chunk_id: chunkId } },
    }),
  );
}
