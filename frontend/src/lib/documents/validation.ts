/** Mismas reglas que el backend (`max_upload_mb` por defecto y extensiones aceptadas). La validación
 * real es la del servidor; esta solo evita subir un archivo que ya se sabe rechazado. */
export const MAX_UPLOAD_MB = 20;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
export const ACCEPTED_EXTENSIONS = [".pdf", ".txt", ".md", ".markdown"] as const;
export const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.join(",");
export const ACCEPTED_LABEL = "PDF, TXT o Markdown (.pdf, .txt, .md, .markdown)";

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Mensaje de error si el archivo no se puede subir; `null` si es válido. */
export function validateFile(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return `Tipo de archivo no admitido. Sube un ${ACCEPTED_LABEL}.`;
  }
  if (file.size === 0) return "El archivo está vacío.";
  if (file.size > MAX_UPLOAD_BYTES) {
    return `El archivo pesa ${formatSize(file.size)} y el máximo es ${MAX_UPLOAD_MB} MB.`;
  }
  return null;
}
