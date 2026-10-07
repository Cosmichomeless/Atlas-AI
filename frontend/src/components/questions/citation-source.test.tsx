import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import type { QuestionCitation } from "@/lib/api/questions";
import { errorResponse, jsonResponse, mockApi } from "@/test/api-mock";

import { CitationSource } from "./citation-source";

const DOC = "00000000-0000-4000-8000-000000000001";
const CHUNK = "00000000-0000-4000-8000-0000000000c1";
const PATH = `GET /api/v1/documents/${DOC}/chunks/${CHUNK}`;

const CITATION: QuestionCitation = {
  label: "S1",
  document_id: DOC,
  filename: "contrato.pdf",
  chunk_id: CHUNK,
  ordinal: 2,
  page: 3,
  section: "Entregas",
  start_line: null,
  end_line: null,
};

const PASSAGE = {
  chunk_id: CHUNK,
  document_id: DOC,
  filename: "contrato.pdf",
  ordinal: 2,
  text: "El plazo de entrega es de diez días.",
  page: 3,
  section: "Entregas",
  start_line: null,
  end_line: null,
};

afterEach(() => {
  cleanup();
});

describe("CitationSource", () => {
  it("muestra documento y ubicación sin pedir nada hasta pulsar", () => {
    const { calls } = mockApi({});
    render(<CitationSource citation={CITATION} />);

    expect(screen.getByRole("button", { name: "[S1] contrato.pdf p. 3 · Entregas" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(calls).toHaveLength(0);
  });

  it("al pulsar enseña el pasaje original y al volver a pulsar lo oculta", async () => {
    mockApi({ [PATH]: () => jsonResponse(200, PASSAGE) });
    render(<CitationSource citation={CITATION} />);
    const user = userEvent.setup();
    const button = screen.getByRole("button");

    await user.click(button);
    expect(await screen.findByText("El plazo de entrega es de diez días.")).toBeInTheDocument();
    expect(button).toHaveAttribute("aria-expanded", "true");

    await user.click(button);
    expect(screen.queryByText("El plazo de entrega es de diez días.")).not.toBeInTheDocument();
  });

  it("indica las líneas en documentos de texto", async () => {
    mockApi({
      [PATH]: () => jsonResponse(200, { ...PASSAGE, page: null, section: null, start_line: 4, end_line: 9 }),
    });
    render(<CitationSource citation={{ ...CITATION, page: null, section: null, start_line: 4, end_line: 9 }} />);

    await userEvent.setup().click(screen.getByRole("button", { name: /líneas 4–9/ }));
    expect(await screen.findByRole("figure")).toHaveTextContent("contrato.pdf · líneas 4–9");
  });

  it("muestra un estado seguro si la fuente ya no existe", async () => {
    mockApi({ [PATH]: () => errorResponse(404, "passage_not_found", "El pasaje ya no está disponible.") });
    render(<CitationSource citation={CITATION} />);

    await userEvent.setup().click(screen.getByRole("button"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("ya no está disponible");
    expect(alert).not.toHaveTextContent("reintentar");
    expect(screen.queryByRole("figure")).not.toBeInTheDocument();
  });

  it("permite reintentar tras un fallo pasajero", async () => {
    let attempts = 0;
    mockApi({
      [PATH]: () =>
        ++attempts === 1 ? errorResponse(503, "unavailable", "Servicio no disponible.") : jsonResponse(200, PASSAGE),
    });
    render(<CitationSource citation={CITATION} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Vuelve a pulsar la cita para reintentar.");

    await user.click(screen.getByRole("button"));
    expect(await screen.findByText("El plazo de entrega es de diez días.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("no pide el pasaje dos veces con pulsaciones seguidas", async () => {
    const { calls } = mockApi({ [PATH]: () => jsonResponse(200, PASSAGE) });
    render(<CitationSource citation={CITATION} />);
    const button = screen.getByRole("button");
    const user = userEvent.setup();

    await user.dblClick(button);
    await screen.findByRole("figure");
    expect(calls.filter((call) => call.path.includes("/chunks/"))).toHaveLength(1);
  });
});
