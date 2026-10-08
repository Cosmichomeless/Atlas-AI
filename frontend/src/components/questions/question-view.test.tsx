import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { errorResponse, jsonResponse, mockApi } from "@/test/api-mock";

import { QuestionView } from "./question-view";

function summary(n: number) {
  return {
    id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    filename: `doc-${n}.pdf`,
    content_type: "application/pdf",
    size_bytes: 2048,
    status: "READY",
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T10:00:00Z",
  };
}

const LIST = () => jsonResponse(200, { items: [summary(1), summary(2)], total: 2, limit: 100, offset: 0 });

const ANSWERED = {
  status: "answered",
  text: "El plazo es de 30 días [S1].",
  abstention_reason: null,
  citations: [
    {
      label: "S1",
      document_id: summary(1).id,
      filename: "doc-1.pdf",
      chunk_id: "00000000-0000-4000-8000-0000000000c1",
      ordinal: 0,
      page: 3,
      section: null,
      start_line: null,
      end_line: null,
    },
  ],
  uncited_statements: [],
  documents: [{ document_id: summary(1).id, filename: "doc-1.pdf" }],
  truncated: false,
  provenance: null,
};

afterEach(() => {
  cleanup();
});

async function ask(text = "¿Cuál es el plazo?") {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/tu pregunta/i), text);
  await user.click(screen.getByRole("button", { name: "Preguntar" }));
  return user;
}

describe("QuestionView", () => {
  it("muestra la respuesta con sus fuentes", async () => {
    const { calls } = mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () => jsonResponse(200, ANSWERED),
    });
    render(<QuestionView />);
    await ask();

    // La marca [S1] se pinta como etiqueta propia: el texto de la respuesta es el mismo, en varios nodos.
    const answer = await screen.findByRole("article", { name: "Respuesta" });
    expect(answer).toHaveTextContent("El plazo es de 30 días [S1].");
    // Una etiqueta en el texto y otra en el botón de la fuente.
    expect(within(answer).getAllByText("[S1]")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /\[S1\] doc-1\.pdf p\. 3/ })).toBeInTheDocument();
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ question: "¿Cuál es el plazo?" });
  });

  it("envía solo los documentos seleccionados", async () => {
    const { calls } = mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () => jsonResponse(200, ANSWERED),
    });
    render(<QuestionView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("checkbox", { name: "doc-2.pdf" }));
    expect(screen.getByText("1 seleccionado.")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/tu pregunta/i), "Hola");
    await user.click(screen.getByRole("button", { name: "Preguntar" }));

    await screen.findByRole("article", { name: "Respuesta" });
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      question: "Hola",
      document_ids: [summary(2).id],
    });
  });

  it("distingue la abstención de una respuesta y de un error", async () => {
    mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () =>
        jsonResponse(200, {
          ...ANSWERED,
          status: "abstained",
          text: null,
          abstention_reason: "insufficient_evidence",
          citations: [],
        }),
    });
    render(<QuestionView />);
    await ask();

    const article = await screen.findByRole("article", { name: "Sin respuesta" });
    expect(article).toHaveTextContent("no tienen información suficiente");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    [409, "index_incompatible", /reindéxalos/],
    [503, "llm_unavailable", /no está disponible/],
    [503, "embedding_unavailable", /no está disponible/],
  ])("muestra el error %i %s como alerta", async (status, code, message) => {
    mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () => errorResponse(status, code, "mensaje del servidor"),
    });
    render(<QuestionView />);
    await ask();

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("muestra el mensaje del servidor ante una pregunta inválida", async () => {
    mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () => errorResponse(422, "question_too_long", "La pregunta es demasiado larga."),
    });
    render(<QuestionView />);
    await ask();

    expect(await screen.findByRole("alert")).toHaveTextContent("La pregunta es demasiado larga.");
  });

  it("avisa de respuesta truncada y de frases sin fuente", async () => {
    mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () =>
        jsonResponse(200, { ...ANSWERED, truncated: true, uncited_statements: ["Esto no tiene fuente."] }),
    });
    render(<QuestionView />);
    await ask();

    expect(await screen.findByText(/puede estar incompleta/)).toBeInTheDocument();
    expect(screen.getByText("Esto no tiene fuente.")).toBeInTheDocument();
  });

  it("no envía dos peticiones mientras hay una en curso", async () => {
    let release: (response: Response) => void = () => {};
    const { calls } = mockApi({
      "GET /api/v1/documents": LIST,
      "POST /api/v1/questions": () => new Promise<Response>((resolve) => (release = resolve)),
    });
    render(<QuestionView />);
    const user = await ask();

    const button = screen.getByRole("button", { name: "Buscando respuesta…" });
    expect(button).toBeDisabled();
    await user.click(button);
    await user.type(screen.getByLabelText(/tu pregunta/i), "{Enter}");
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);

    release(jsonResponse(200, ANSWERED));
    await screen.findByRole("article", { name: "Respuesta" });
    expect(screen.getByRole("button", { name: "Preguntar" })).toBeEnabled();
  });

  it("no permite preguntar con el texto vacío", async () => {
    mockApi({ "GET /api/v1/documents": LIST });
    render(<QuestionView />);
    await screen.findByRole("checkbox", { name: "doc-1.pdf" });
    expect(screen.getByRole("button", { name: "Preguntar" })).toBeDisabled();
  });

  it("guía a subir documentos cuando no hay ninguno listo", async () => {
    mockApi({ "GET /api/v1/documents": () => jsonResponse(200, { items: [], total: 0, limit: 100, offset: 0 }) });
    render(<QuestionView />);

    expect(await screen.findByRole("link", { name: "Sube uno" })).toHaveAttribute("href", "/documents");
    expect(screen.queryByRole("group", { name: "Documentos" })).not.toBeInTheDocument();
  });

  it("avisa si no se pudieron cargar los documentos", async () => {
    mockApi({ "GET /api/v1/documents": () => errorResponse(500, "internal_error", "Error interno.") });
    render(<QuestionView />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Error interno."));
  });
});
