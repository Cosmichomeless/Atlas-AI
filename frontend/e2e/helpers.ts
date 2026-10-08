import { expect } from "@playwright/test";
import type { Page } from "@playwright/test";

export const API_URL = "http://127.0.0.1:8765";
export const PASSWORD = "e2e-password-123";

/** Texto de prueba: la respuesta del LLM fake sale de sus frases, y la pregunta debe compartir palabras. */
export const CONTRACT = [
  "La renta mensual del alquiler es de mil doscientos euros.",
  "El inquilino entrega la fianza el primer día del contrato.",
  "El proveedor del agua factura cada trimestre.",
].join("\n");

let counter = 0;

export function uniqueEmail(label: string): string {
  counter += 1;
  return `${label}-${Date.now()}-${counter}@example.com`;
}

/** Crea una cuenta desde la interfaz y espera a llegar a la biblioteca de documentos. */
export async function register(page: Page, email: string): Promise<void> {
  await page.goto("/register");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Contraseña").fill(PASSWORD);
  await page.getByRole("button", { name: "Crear cuenta" }).click();
  await expect(page).toHaveURL(/\/documents$/);
  await expect(page.getByLabel("Subir un documento")).toBeVisible();
}

/** Sube un archivo de texto y espera a que la ingestión lo deje en «Listo». */
export async function uploadAndWaitReady(page: Page, name: string, content: string): Promise<void> {
  await page.getByLabel("Subir un documento").setInputFiles({
    name,
    mimeType: "text/plain",
    buffer: Buffer.from(content, "utf-8"),
  });
  await page.getByRole("button", { name: "Subir", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "se está procesando" })).toBeVisible();
  const row = page.getByRole("listitem").filter({ hasText: name });
  await expect(row.getByText("Listo", { exact: true })).toBeVisible({ timeout: 45_000 });
}

export async function ask(page: Page, question: string): Promise<void> {
  await page.getByLabel("Tu pregunta").fill(question);
  await page.getByRole("button", { name: "Preguntar", exact: true }).click();
}
