import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AskPage from "@/app/(app)/ask/page";
import DocumentsPage from "@/app/(app)/documents/page";
import { POLL_INTERVAL_MS } from "@/components/documents/document-library";
import { ApiError } from "@/lib/api/errors";
import { errorResponse, jsonResponse, mockApi } from "@/test/api-mock";

const upload = vi.hoisted(() => ({ uploadDocument: vi.fn() }));
vi.mock("@/lib/api/upload", () => upload);

/** Recorridos de usuario sobre las páginas reales, con la API simulada (sin red ni backend). */

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

function summary(n: number, status: string) {
  return {
    id: id(n),
    filename: `doc-${n}.pdf`,
    content_type: "application/pdf",
    size_bytes: 2048,
    status,
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T10:00:00Z",
  };
}

const list = (items: ReturnType<typeof summary>[]) =>
  jsonResponse(200, { items, total: items.length, limit: 20, offset: 0 });

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("subida e ingestión", () => {
  beforeEach(() => {
    upload.uploadDocument.mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it("una subida fallida se corrige, el documento queda en curso y termina listo", async () => {
    let documents: ReturnType<typeof summary>[] = [];
    mockApi({ "GET /api/v1/documents": () => list(documents) });
    upload.uploadDocument
      .mockRejectedValueOnce(new ApiError(413, "file_too_large", "El archivo supera los 20 MB."))
      .mockImplementationOnce(async () => {
        documents = [summary(1, "UPLOADED")];
        return documents[0];
      });
    render(<DocumentsPage />);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await screen.findByText(/Todavía no/);
    const file = new File(["%PDF-1.4"], "informe.pdf", { type: "application/pdf" });

    // 1. La subida falla: se explica y no aparece ningún documento.
    await user.upload(screen.getByLabelText("Subir un documento"), file);
    await user.click(screen.getByRole("button", { name: "Subir" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("supera los 20 MB");
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();

    // 2. Se reintenta: el documento aparece en cola y luego pasa por procesando hasta listo.
    await user.click(screen.getByRole("button", { name: "Subir" }));
    expect(await screen.findByText("En cola")).toBeInTheDocument();

    documents = [summary(1, "PROCESSING")];
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findByText("Procesando")).toBeInTheDocument();
    expect(screen.queryByText("Listo")).not.toBeInTheDocument();

    documents = [summary(1, "READY")];
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findByText("Listo")).toBeInTheDocument();
    expect(upload.uploadDocument).toHaveBeenCalledTimes(2);
  });

  it("un documento que falla al procesarse muestra su causa", async () => {
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "FAILED")]),
      [`GET /api/v1/documents/${id(1)}`]: () =>
        jsonResponse(200, {
          ...summary(1, "FAILED"),
          error_summary: "El documento no contiene texto extraíble.",
          attempts: 3,
          processing_started_at: null,
          processed_at: null,
        }),
    });
    render(<DocumentsPage />);

    expect(await screen.findByText("Falló")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /ver causa/i }));
    expect(await screen.findByText("El documento no contiene texto extraíble.")).toBeInTheDocument();
  });
});

describe("pregunta, abstención y citas", () => {
  const citation = (n: number) => ({
    label: `S${n}`,
    document_id: id(n),
    filename: `doc-${n}.pdf`,
    chunk_id: id(100 + n),
    ordinal: n,
    page: n,
    section: null,
    start_line: null,
    end_line: null,
  });
  const passage = (n: number) => ({
    chunk_id: id(100 + n),
    document_id: id(n),
    filename: `doc-${n}.pdf`,
    ordinal: n,
    text: `Texto original del documento ${n}.`,
    page: n,
    section: null,
    start_line: null,
    end_line: null,
  });
  const ANSWERED = {
    status: "answered",
    text: "Una cosa [S1] y otra [S2].",
    abstention_reason: null,
    citations: [citation(1), citation(2)],
    uncited_statements: [],
    documents: [],
    truncated: false,
    provenance: null,
  };
  const ABSTAINED = {
    ...ANSWERED,
    status: "abstained",
    text: null,
    abstention_reason: "no_relevant_chunks",
    citations: [],
  };

  const passageRoute = (n: number) => `GET /api/v1/documents/${id(n)}/chunks/${id(100 + n)}`;

  async function ask(text: string) {
    const user = userEvent.setup();
    const box = await screen.findByLabelText(/tu pregunta/i);
    await user.clear(box);
    await user.type(box, text);
    await user.click(screen.getByRole("button", { name: "Preguntar" }));
    return user;
  }

  it("tras una abstención, una pregunta mejor obtiene respuesta y la cita abre su fuente", async () => {
    const answers = [ABSTAINED, ANSWERED];
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "READY"), summary(2, "READY")]),
      "POST /api/v1/questions": () => jsonResponse(200, answers.shift()),
      [passageRoute(1)]: () => jsonResponse(200, passage(1)),
      [passageRoute(2)]: () => jsonResponse(200, passage(2)),
    });
    render(<AskPage />);

    // Abstención: bloque propio, sin texto de respuesta ni alerta de error.
    await ask("¿Cuál es la capital de Marte?");
    const none = await screen.findByRole("article", { name: "Sin respuesta" });
    expect(none).toHaveTextContent("No he encontrado nada relacionado");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    // Respuesta: sustituye a la abstención y lista las dos fuentes.
    const user = await ask("¿Qué dicen los documentos?");
    const answer = await screen.findByRole("article", { name: "Respuesta" });
    expect(screen.queryByRole("article", { name: "Sin respuesta" })).not.toBeInTheDocument();

    // La cita [S2] abre el pasaje del documento 2 y no el del 1.
    await user.click(within(answer).getByRole("button", { name: /\[S2\] doc-2\.pdf/ }));
    expect(await screen.findByText("Texto original del documento 2.")).toBeInTheDocument();
    expect(screen.queryByText("Texto original del documento 1.")).not.toBeInTheDocument();
    expect(within(answer).getByRole("button", { name: /\[S1\]/ })).toHaveAttribute("aria-expanded", "false");

    // Y la [S1] abre el suyo, sin cerrar el anterior.
    await user.click(within(answer).getByRole("button", { name: /\[S1\] doc-1\.pdf/ }));
    expect(await screen.findByText("Texto original del documento 1.")).toBeInTheDocument();
    expect(screen.getByText("Texto original del documento 2.")).toBeInTheDocument();
  });

  it("una fuente borrada avisa solo en su cita; las demás siguen abriéndose", async () => {
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "READY"), summary(2, "READY")]),
      "POST /api/v1/questions": () => jsonResponse(200, ANSWERED),
      [passageRoute(1)]: () => errorResponse(404, "passage_not_found", "El pasaje ya no está disponible."),
      [passageRoute(2)]: () => jsonResponse(200, passage(2)),
    });
    render(<AskPage />);
    const user = await ask("¿Qué dicen los documentos?");
    const answer = await screen.findByRole("article", { name: "Respuesta" });

    await user.click(within(answer).getByRole("button", { name: /\[S1\]/ }));
    expect(await within(answer).findByRole("alert")).toHaveTextContent("ya no está disponible");

    await user.click(within(answer).getByRole("button", { name: /\[S2\]/ }));
    expect(await screen.findByText("Texto original del documento 2.")).toBeInTheDocument();
    expect(within(answer).getAllByRole("alert")).toHaveLength(1);
  });
});
