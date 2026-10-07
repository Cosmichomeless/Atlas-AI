interface Located {
  page: number | null;
  section: string | null;
  start_line: number | null;
  end_line: number | null;
}

/** Ubicación legible de un fragmento: «p. 3 · Sección · líneas 4–9»; vacío si no hay ninguna. */
export function describeLocation({ page, section, start_line, end_line }: Located): string {
  const parts: string[] = [];
  if (page !== null) parts.push(`p. ${page}`);
  if (section) parts.push(section);
  if (start_line !== null && end_line !== null) {
    parts.push(start_line === end_line ? `línea ${start_line}` : `líneas ${start_line}–${end_line}`);
  }
  return parts.join(" · ");
}
