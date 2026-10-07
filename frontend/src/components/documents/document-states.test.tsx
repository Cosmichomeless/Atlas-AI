import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NO_CONTENT, errorResponse, jsonResponse, mockApi } from "@/test/api-mock";

import { DocumentLibrary, POLL_INTERVAL_MS } from "./document-library";

vi.mock("@/lib/api/upload", () => ({ uploadDocument: vi.fn() }));

function summary(n: number, status: string) {
  return {
    id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
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

const listCalls = (calls: { method: string; path: string }[]) =>
  calls.filter((c) => c.method === "GET" && c.path === "/api/v1/documents").length;

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("actualización del estado", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it("refresca hasta que el documento llega a READY y entonces deja de consultar", async () => {
    const states = ["UPLOADED", "PROCESSING", "READY"];
    let request = 0;
    const { calls } = mockApi({
      "GET /api/v1/documents": () => list([summary(1, states[Math.min(request++, 2)]!)]),
    });
    render(<DocumentLibrary />);

    expect(await screen.findByText("En cola")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findByText("Procesando")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findByText("Listo")).toBeInTheDocument();

    const before = listCalls(calls);
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5));
    expect(listCalls(calls)).toBe(before);
  });

  it("también termina en FAILED", async () => {
    let request = 0;
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, request++ === 0 ? "PROCESSING" : "FAILED")]),
    });
    render(<DocumentLibrary />);

    await screen.findByText("Procesando");
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));

    expect(await screen.findByText("Falló")).toBeInTheDocument();
  });

  it("no consulta de nuevo si todos los documentos ya están terminados", async () => {
    const { calls } = mockApi({ "GET /api/v1/documents": () => list([summary(1, "READY"), summary(2, "FAILED")]) });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5));

    expect(listCalls(calls)).toBe(1);
  });

  it("un fallo de red en el refresco no tapa la lista y se reintenta", async () => {
    let request = 0;
    mockApi({
      "GET /api/v1/documents": () => {
        request++;
        if (request === 2) throw new TypeError("network down");
        return list([summary(1, request === 1 ? "PROCESSING" : "READY")]);
      },
    });
    render(<DocumentLibrary />);
    await screen.findByText("Procesando");

    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("doc-1.pdf")).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findByText("Listo")).toBeInTheDocument();
  });

  it("cancela el temporizador al desmontar", async () => {
    const { calls } = mockApi({ "GET /api/v1/documents": () => list([summary(1, "PROCESSING")]) });
    const { unmount } = render(<DocumentLibrary />);
    await screen.findByText("Procesando");

    unmount();
    const before = listCalls(calls);
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3));

    expect(listCalls(calls)).toBe(before);
  });
});

describe("documento fallido", () => {
  const detail = (error_summary: string | null) =>
    jsonResponse(200, {
      ...summary(1, "FAILED"),
      error_summary,
      attempts: 3,
      processing_started_at: null,
      processed_at: null,
    });

  it("muestra la causa registrada por el servidor", async () => {
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "FAILED")]),
      "GET /api/v1/documents/00000000-0000-4000-8000-000000000001": () =>
        detail("El documento no contiene texto extraíble."),
    });
    render(<DocumentLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Ver causa" }));

    expect(await screen.findByText(/no contiene texto extraíble/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ver causa" })).not.toBeInTheDocument();
  });

  it("explica que no hay causa registrada en lugar de quedarse vacío", async () => {
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "FAILED")]),
      "GET /api/v1/documents/00000000-0000-4000-8000-000000000001": () => detail(null),
    });
    render(<DocumentLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Ver causa" }));

    expect(await screen.findByText(/no se registró la causa/)).toBeInTheDocument();
  });

  it("avisa si no se pudo cargar la causa y permite reintentar", async () => {
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "FAILED")]),
      "GET /api/v1/documents/00000000-0000-4000-8000-000000000001": () =>
        errorResponse(500, "internal_error", "Fallo interno."),
    });
    render(<DocumentLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Ver causa" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Fallo interno.");
    expect(screen.getByRole("button", { name: "Ver causa" })).toBeEnabled();
  });

  it("no ofrece «Ver causa» en documentos que no han fallado", async () => {
    mockApi({ "GET /api/v1/documents": () => list([summary(1, "READY")]) });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    expect(screen.queryByRole("button", { name: "Ver causa" })).not.toBeInTheDocument();
  });
});

describe("eliminar documentos", () => {
  const ID1 = "/api/v1/documents/00000000-0000-4000-8000-000000000001";

  it("pide confirmación y el documento desaparece al eliminarlo", async () => {
    let documents = [summary(1, "READY"), summary(2, "READY")];
    const { calls } = mockApi({
      "GET /api/v1/documents": () => list(documents),
      [`DELETE ${ID1}`]: () => {
        documents = documents.slice(1);
        return NO_CONTENT();
      },
    });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    await userEvent.click(screen.getByRole("button", { name: "Eliminar doc-1.pdf" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Sí, eliminar" }));

    await vi.waitFor(() => expect(screen.queryByText("doc-1.pdf")).not.toBeInTheDocument());
    expect(screen.getByText("doc-2.pdf")).toBeInTheDocument();
    expect(screen.getByText("1–1 de 1")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(1);
  });

  it("cancelar no elimina nada", async () => {
    const { calls } = mockApi({ "GET /api/v1/documents": () => list([summary(1, "READY")]) });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    await userEvent.click(screen.getByRole("button", { name: "Eliminar doc-1.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancelar" }));

    expect(screen.getByText("doc-1.pdf")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });

  it("un 409 mantiene el documento y explica que se vuelva a intentar", async () => {
    mockApi({
      "GET /api/v1/documents": () => list([summary(1, "READY")]),
      [`DELETE ${ID1}`]: () =>
        errorResponse(409, "document_processing", "El documento se está procesando; inténtalo de nuevo en unos instantes."),
    });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    await userEvent.click(screen.getByRole("button", { name: "Eliminar doc-1.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, eliminar" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("inténtalo de nuevo en unos instantes");
    expect(screen.getByText("doc-1.pdf")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Eliminar doc-1.pdf" })).toBeEnabled();
  });

  it("un 404 (ya borrado en otro sitio) también lo quita de la lista", async () => {
    let documents = [summary(1, "READY")];
    mockApi({
      "GET /api/v1/documents": () => list(documents),
      [`DELETE ${ID1}`]: () => {
        documents = [];
        return errorResponse(404, "document_not_found", "Documento no encontrado.");
      },
    });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    await userEvent.click(screen.getByRole("button", { name: "Eliminar doc-1.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, eliminar" }));

    expect(await screen.findByText(/Todavía no has subido/)).toBeInTheDocument();
  });

  it("no permite eliminar mientras se procesa", async () => {
    mockApi({ "GET /api/v1/documents": () => list([summary(1, "PROCESSING")]) });
    render(<DocumentLibrary />);
    await screen.findByText("doc-1.pdf");

    const row = screen.getByText("doc-1.pdf").closest("li")!;
    expect(within(row).getByRole("button", { name: "Eliminar doc-1.pdf" })).toBeDisabled();
  });
});
