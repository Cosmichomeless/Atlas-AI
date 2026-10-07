import type { components } from "./schema";
import { api, unwrap } from "./client";

export type QuestionResponse = components["schemas"]["QuestionResponse"];
export type QuestionCitation = components["schemas"]["QuestionCitation"];
export type AbstentionReason = NonNullable<QuestionResponse["abstention_reason"]>;

/** Pregunta a los documentos listos; sin `documentIds` el backend usa todos los del usuario. */
export function askQuestion(question: string, documentIds?: string[]): Promise<QuestionResponse> {
  const body = documentIds && documentIds.length > 0 ? { question, document_ids: documentIds } : { question };
  return unwrap(api.POST("/api/v1/questions", { body }));
}
