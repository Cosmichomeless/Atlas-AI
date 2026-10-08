import { expect, test } from "@playwright/test";

import { API_URL, CONTRACT, ask, register, uniqueEmail, uploadAndWaitReady } from "./helpers";

test.describe("acceso no autorizado", () => {
  for (const path of ["/documents", "/ask"]) {
    test(`sin sesión ${path} redirige al acceso`, async ({ page }) => {
      await page.goto(path);
      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByRole("heading", { name: "Entrar" })).toBeVisible();
    });
  }

  test("la API rechaza las peticiones sin sesión", async ({ request }) => {
    const response = await request.get(`${API_URL}/api/v1/documents`);
    expect(response.status()).toBe(401);
  });

  test("otro usuario no ve ni cita los documentos ajenos", async ({ browser, page }) => {
    // Alice sube un documento y obtiene una respuesta con cita.
    await register(page, uniqueEmail("alice"));
    await uploadAndWaitReady(page, "privado.txt", CONTRACT);
    await page.goto("/ask");
    const asked = page.waitForResponse((r) => r.url().endsWith("/api/v1/questions") && r.request().method() === "POST");
    await ask(page, "¿Cuál es la renta mensual del alquiler?");
    const answer = (await (await asked).json()) as {
      citations: { document_id: string; chunk_id: string }[];
    };
    const [citation] = answer.citations;
    expect(citation).toBeDefined();

    // Bob, con su propia sesión, recibe el mismo 404 que si no existieran.
    const bobContext = await browser.newContext();
    try {
      const bob = await bobContext.newPage();
      await register(bob, uniqueEmail("bob"));

      const document = await bobContext.request.get(`${API_URL}/api/v1/documents/${citation.document_id}`);
      expect(document.status()).toBe(404);
      const passage = await bobContext.request.get(
        `${API_URL}/api/v1/documents/${citation.document_id}/chunks/${citation.chunk_id}`,
      );
      expect(passage.status()).toBe(404);

      await expect(bob.getByText("privado.txt")).toHaveCount(0);
      await bob.goto("/ask");
      await ask(bob, "¿Cuál es la renta mensual del alquiler?");
      await expect(bob.getByRole("article", { name: "Sin respuesta" })).toBeVisible();
    } finally {
      await bobContext.close();
    }
  });
});
