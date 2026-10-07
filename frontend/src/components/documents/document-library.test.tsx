import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { errorResponse, jsonResponse, mockApi } from "@/test/api-mock";

import { DocumentLibrary } from "./document-library";

const upload = vi.hoisted(() => ({ uploadDocument: vi.fn() }));
vi.mock("@/lib/api/upload", () => upload);

function summary(n: number, status = "READY") {
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

function list(items: ReturnType<typeof summary>[], total = items.length, offset = 0) {
  return jsonResponse(200, { items, total, limit: 20, offset });
}

beforeEach(() => {
  upload.uploadDocument.mockReset();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DocumentLibrary", () => {
  it("muestra solo los documentos que devuelve la API del usuario", async () => {
    mockApi({ "GET /api/v1/documents": () => list([summary(1), summary(2, "PROCESSING")]) });
    render(<DocumentLibrary />);

    expect(await screen.findByText("doc-1.pdf")).toBeInTheDocument();
    expect(screen.getByText("doc-2.pdf")).toBeInTheDocument();
    expect(screen.getByText("Listo")).toBeInTheDocument();
    expect(screen.getByText("Procesando")).toBeInTheDocument();
    expect(screen.getByText("1–2 de 2")).toBeInTheDocument();
  });

  it("muestra un estado vacío cuando no hay documentos", async () => {
    mockApi({ "GET /api/v1/documents": () => list([]) });
    render(<DocumentLibrary />);

    expect(await screen.findByText(/Todavía no has subido ningún documento/)).toBeInTheDocument();
  });

  it("muestra el error de carga y permite reintentar", async () => {
    let attempts = 0;
    mockApi({
      "GET /api/v1/documents": () =>
        ++attempts === 1 ? errorResponse(500, "internal_error", "Fallo interno.") : list([summary(1)]),
    });
    render(<DocumentLibrary />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Fallo interno.");
    await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));

    expect(await screen.findByText("doc-1.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pagina con offset", async () => {
    const { calls } = mockApi({
      "GET /api/v1/documents": (_call, request) => {
        const offset = Number(new URL(request.url).searchParams.get("offset"));
        return offset === 0 ? list([summary(1)], 21, 0) : list([summary(21)], 21, 20);
      },
    });
    render(<DocumentLibrary />);

    await screen.findByText("doc-1.pdf");
    expect(screen.getByRole("button", { name: "Anterior" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));

    expect(await screen.findByText("doc-21.pdf")).toBeInTheDocument();
    expect(screen.getByText("21–21 de 21")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Siguiente" })).toBeDisabled();
    expect(calls.filter((c) => c.method === "GET")).toHaveLength(2);
  });

  describe("subida", () => {
    const pdf = () => new File(["x"], "informe.pdf", { type: "application/pdf" });

    it("rechaza un tipo no admitido sin llamar a la API", async () => {
      mockApi({ "GET /api/v1/documents": () => list([]) });
      render(<DocumentLibrary />);
      await screen.findByText(/Todavía no/);

      await userEvent.upload(screen.getByLabelText("Subir un documento"), new File(["x"], "a.docx"), {
        applyAccept: false,
      });

      expect(screen.getByRole("alert")).toHaveTextContent("Tipo de archivo no admitido");
      expect(screen.getByRole("button", { name: "Subir" })).toBeDisabled();
      expect(upload.uploadDocument).not.toHaveBeenCalled();
    });

    it("rechaza un archivo de más de 20 MB", async () => {
      mockApi({ "GET /api/v1/documents": () => list([]) });
      render(<DocumentLibrary />);
      await screen.findByText(/Todavía no/);
      const big = new File(["x"], "grande.pdf");
      Object.defineProperty(big, "size", { value: 21 * 1024 * 1024 });

      await userEvent.upload(screen.getByLabelText("Subir un documento"), big);

      expect(screen.getByRole("alert")).toHaveTextContent("el máximo es 20 MB");
      expect(upload.uploadDocument).not.toHaveBeenCalled();
    });

    it("muestra el progreso y recarga la lista al terminar", async () => {
      let documents = [] as ReturnType<typeof summary>[];
      mockApi({ "GET /api/v1/documents": () => list(documents) });
      let finish!: (value: unknown) => void;
      upload.uploadDocument.mockImplementation(
        (_file: File, options: { onProgress: (fraction: number) => void }) =>
          new Promise((resolve) => {
            options.onProgress(0.4);
            finish = resolve;
          }),
      );
      render(<DocumentLibrary />);
      await screen.findByText(/Todavía no/);

      await userEvent.upload(screen.getByLabelText("Subir un documento"), pdf());
      await userEvent.click(screen.getByRole("button", { name: "Subir" }));

      const bar = await screen.findByRole("progressbar", { name: "Progreso de la subida" });
      expect(bar).toHaveAttribute("value", "40");
      expect(screen.getByText("40 %")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Subiendo…" })).toBeDisabled();

      documents = [summary(1, "UPLOADED")];
      finish({ ...summary(1, "UPLOADED"), filename: "informe.pdf" });

      expect(await screen.findByRole("status")).toHaveTextContent("«informe.pdf» se ha subido");
      expect(await screen.findByText("doc-1.pdf")).toBeInTheDocument();
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });

    it("no permite dos subidas a la vez", async () => {
      mockApi({ "GET /api/v1/documents": () => list([]) });
      upload.uploadDocument.mockReturnValue(new Promise(() => {}));
      render(<DocumentLibrary />);
      await screen.findByText(/Todavía no/);

      await userEvent.upload(screen.getByLabelText("Subir un documento"), pdf());
      await userEvent.click(screen.getByRole("button", { name: "Subir" }));
      await userEvent.click(screen.getByRole("button", { name: "Subiendo…" }));

      expect(upload.uploadDocument).toHaveBeenCalledOnce();
    });

    it("muestra el error del servidor y permite reintentar", async () => {
      mockApi({ "GET /api/v1/documents": () => list([]) });
      const { ApiError } = await import("@/lib/api/errors");
      upload.uploadDocument.mockRejectedValueOnce(
        new ApiError(413, "file_too_large", "El archivo supera los 20 MB."),
      );
      render(<DocumentLibrary />);
      await screen.findByText(/Todavía no/);

      await userEvent.upload(screen.getByLabelText("Subir un documento"), pdf());
      await userEvent.click(screen.getByRole("button", { name: "Subir" }));

      const alert = await screen.findByRole("alert");
      expect(within(alert.closest("form")!).getByRole("alert")).toHaveTextContent("supera los 20 MB");
      await waitFor(() => expect(screen.getByRole("button", { name: "Subir" })).toBeEnabled());
    });
  });
});
