import type { DocumentStatus } from "@/lib/api/documents";

export const STATUS_LABEL: Record<DocumentStatus, string> = {
  UPLOADED: "En cola",
  PROCESSING: "Procesando",
  READY: "Listo",
  FAILED: "Falló",
};

/** Estados que todavía van a cambiar solos: la lista debe seguir refrescándose. */
export function isPending(status: DocumentStatus): boolean {
  return status === "UPLOADED" || status === "PROCESSING";
}
