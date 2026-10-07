import { ApiError } from "./errors";

/** Mensaje para el usuario: el del servidor si lo hay, uno genérico si no hubo respuesta útil. */
export function describeError(error: unknown, fallback = "Algo salió mal. Inténtalo de nuevo."): string {
  if (error instanceof ApiError) {
    if (error.code === "network_error") return "No se pudo conectar con el servidor. Revisa tu conexión.";
    if (error.code === "unexpected_error") return fallback;
    return error.message;
  }
  return fallback;
}

/** Errores de validación por campo: `{ email: "…" }` a partir de `details[].loc`. */
export function fieldErrors(error: unknown): Record<string, string> {
  const result: Record<string, string> = {};
  if (!(error instanceof ApiError) || !error.details) return result;
  for (const detail of error.details) {
    const field = detail.loc[detail.loc.length - 1];
    if (typeof field === "string" && !(field in result)) result[field] = detail.message;
  }
  return result;
}
