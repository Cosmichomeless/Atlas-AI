import { expect, test } from "@playwright/test";

import { CONTRACT, ask, register, uniqueEmail, uploadAndWaitReady } from "./helpers";

test.describe("recorrido subida → respuesta con cita", () => {
  test("sube un documento, espera a que esté listo, pregunta y abre la cita", async ({ page }) => {
    await register(page, uniqueEmail("viajero"));
    await uploadAndWaitReady(page, "contrato.txt", CONTRACT);

    await page.getByRole("link", { name: "Preguntar" }).click();
    await expect(page).toHaveURL(/\/ask$/);
    await ask(page, "¿Cuál es la renta mensual del alquiler?");

    const answer = page.getByRole("article", { name: "Respuesta", exact: true });
    await expect(answer).toContainText("renta mensual del alquiler es de mil doscientos euros");

    const source = answer.getByRole("button", { name: /contrato\.txt/ });
    await expect(source).toBeVisible();
    await source.click();

    const passage = answer.getByRole("figure");
    await expect(passage).toContainText("contrato.txt");
    await expect(passage.getByRole("blockquote")).toContainText("La renta mensual del alquiler");
  });

  test("se abstiene cuando los documentos no tienen la respuesta", async ({ page }) => {
    await register(page, uniqueEmail("abstencion"));
    await uploadAndWaitReady(page, "contrato.txt", CONTRACT);

    await page.goto("/ask");
    await ask(page, "¿Qué altura tiene el Everest?");

    const abstention = page.getByRole("article", { name: "Sin respuesta" });
    await expect(abstention.getByRole("heading", { name: "No hay respuesta" })).toBeVisible();
    await expect(page.getByRole("article", { name: "Respuesta", exact: true })).toHaveCount(0);
  });
});
