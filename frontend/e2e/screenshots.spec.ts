import { expect, test } from "@playwright/test";

import { ask, register, uploadAndWaitReady } from "./helpers";

/**
 * Genera las capturas del README (`docs/screenshots/`). No es una prueba: solo se ejecuta con
 * `npm run screenshots` (que fija CAPTURE_SCREENSHOTS=1), contra el mismo stack real que los E2E
 * (Next compilado + API + worker + PostgreSQL con pgvector, proveedores `fake`), con datos
 * ficticios (dominio `example.com`) para que el resultado sea reproducible.
 */
const OUT = "../docs/screenshots";
const ACCOUNT = "ana@example.com";

const TELEWORK = [
  "Cada empleada o empleado puede teletrabajar hasta tres días por semana.",
  "Los martes y los jueves son días de presencia obligatoria en la oficina para las reuniones de equipo.",
  "La empresa abona una compensación de cincuenta euros al mes por gastos de conexión y suministros.",
  "La autorización se revisa cada seis meses y puede retirarse con un preaviso de quince días.",
].join("\n");

const EXPENSES = [
  "La manutención tiene un límite de cuarenta y cinco euros por día en territorio nacional.",
  "Las facturas se escanean y se suben a la aplicación Gastos en un plazo máximo de treinta días naturales.",
  "Las solicitudes aprobadas antes del día diez de cada mes se pagan en la nómina de ese mismo mes.",
  "El alojamiento tiene un límite de ciento diez euros por noche en España.",
].join("\n");

const SECURITY = [
  "Las contraseñas deben tener al menos catorce caracteres y no pueden reutilizarse entre servicios.",
  "El segundo factor de autenticación es obligatorio en el correo, la VPN y el repositorio de código.",
  "Los accesos de producción caducan a los noventa días y deben renovarse con una justificación.",
  "Un incidente de nivel uno exige respuesta de la persona de guardia en menos de cinco minutos.",
].join("\n");

test.describe("capturas del README", () => {
  test.skip(!process.env.CAPTURE_SCREENSHOTS, "solo con `npm run screenshots`");
  test.use({ viewport: { width: 1280, height: 800 } });

  test("genera las capturas", async ({ page, browser }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("documentos");
    await page.screenshot({ path: `${OUT}/01-portada.png` });

    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Entrar" })).toBeVisible();
    await page.screenshot({ path: `${OUT}/02-acceso.png` });

    await register(page, ACCOUNT);
    await uploadAndWaitReady(page, "politica-teletrabajo.txt", TELEWORK);
    await uploadAndWaitReady(page, "guia-gastos.txt", EXPENSES);
    await uploadAndWaitReady(page, "normas-seguridad.txt", SECURITY);
    // Se recarga para quitar el aviso de subida y mostrar solo el estado final de la biblioteca.
    await page.reload();
    await expect(page.getByRole("listitem").filter({ hasText: "normas-seguridad.txt" })).toBeVisible();
    await expect(page.getByText("Listo", { exact: true })).toHaveCount(3);
    await page.screenshot({ path: `${OUT}/03-documentos.png` });

    await page.goto("/ask");
    await ask(page, "¿Cuántos días por semana puedo teletrabajar?");
    const answer = page.getByRole("article", { name: "Respuesta", exact: true });
    await expect(answer).toContainText("tres días por semana");
    await answer.getByRole("button", { name: /politica-teletrabajo\.txt/ }).click();
    await expect(answer.getByRole("figure")).toBeVisible();
    await page.waitForTimeout(400); // deja terminar la animación de apertura del pasaje
    await page.evaluate(() => window.scrollTo(0, 0)); // la cabecera es fija: sin esto sale desplazada
    await page.screenshot({ path: `${OUT}/04-respuesta-con-cita.png`, fullPage: true });

    await page.goto("/ask");
    await ask(page, "¿Cuál es la capital de Australia?");
    await expect(page.getByRole("heading", { name: "No hay respuesta" })).toBeVisible();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${OUT}/05-sin-respuesta.png`, fullPage: true });

    // Vista móvil con la sesión ya iniciada.
    const mobile = await browser.newContext({
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 2,
      locale: "es-ES",
      storageState: await page.context().storageState(),
    });
    try {
      const phone = await mobile.newPage();
      await phone.goto("/ask");
      await ask(phone, "¿Cuál es el plazo para subir las facturas?");
      const reply = phone.getByRole("article", { name: "Respuesta", exact: true });
      await expect(reply).toContainText("treinta días naturales");
      await phone.waitForTimeout(400);
      await phone.evaluate(() => window.scrollTo(0, 0));
      await phone.screenshot({ path: `${OUT}/06-movil.png`, fullPage: true });
    } finally {
      await mobile.close();
    }
  });
});
