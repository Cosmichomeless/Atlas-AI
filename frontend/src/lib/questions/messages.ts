import { ApiError } from "@/lib/api/errors";
import { describeError } from "@/lib/api/messages";
import type { AbstentionReason } from "@/lib/api/questions";

/** Longitud máxima por defecto de una pregunta (`QUESTION_MAX_CHARS`); el servidor es quien decide. */
export const QUESTION_MAX_CHARS = 1000;

export const ABSTENTION_MESSAGE: Record<AbstentionReason, string> = {
  no_relevant_chunks: "No he encontrado nada relacionado con tu pregunta en los documentos seleccionados.",
  insufficient_evidence: "Los documentos no tienen información suficiente para responder con seguridad.",
  no_valid_citations: "No he podido respaldar una respuesta con fuentes verificables, así que prefiero no darla.",
};

export const ABSTENTION_FALLBACK = "No hay evidencia suficiente en los documentos para responder.";

/** Mensaje de error para quien pregunta; distingue lo que puede arreglar de lo que debe esperar. */
export function describeQuestionError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "index_incompatible":
        return "Tus documentos se indexaron con otro modelo de embeddings: reindéxalos para poder preguntar.";
      case "embedding_unavailable":
      case "llm_unavailable":
        return "El servicio de respuestas no está disponible ahora mismo. Inténtalo de nuevo en unos minutos.";
    }
  }
  return describeError(error, "No se pudo responder a tu pregunta.");
}
