import { defineConfig, devices } from "@playwright/test";

/**
 * Pruebas E2E del recorrido subida → READY → pregunta → cita contra el stack real
 * (Next compilado + FastAPI + worker + Postgres) con proveedores fake: sin red ni credenciales.
 *
 * Requiere un Postgres con pgvector y `E2E_DATABASE_URL` apuntando a una base cuyo nombre termine
 * en `_e2e` (el launcher la borra y recrea en cada ejecución). Ver `frontend/README.md`.
 */
const API_PORT = 8765;
const WEB_PORT = 3765;
const API_URL = `http://127.0.0.1:${API_PORT}`;
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

const DATABASE_URL =
  process.env.E2E_DATABASE_URL ?? "postgresql+psycopg://atlas:atlas_dev_password@localhost:5433/atlas_e2e";

export default defineConfig({
  testDir: "./e2e",
  // Una sola base compartida y un solo worker de ingestión: los casos se ejecutan en serie.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  timeout: 60_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: WEB_URL,
    locale: "es-ES",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: process.env.E2E_BACKEND_COMMAND ?? "uv run python scripts/e2e_backend.py",
      cwd: "../backend",
      url: `${API_URL}/health`,
      env: {
        E2E_DATABASE_URL: DATABASE_URL,
        E2E_API_PORT: String(API_PORT),
        FRONTEND_ORIGIN: WEB_URL,
      },
      // La base de datos tiene que empezar limpia: nunca se reutiliza un servidor ya arrancado.
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // `NEXT_PUBLIC_API_BASE_URL` se fija al compilar, por eso el build forma parte del arranque.
      command: `npm run build && npm run start -- --hostname 127.0.0.1 --port ${WEB_PORT}`,
      url: `${WEB_URL}/login`,
      env: { NEXT_PUBLIC_API_BASE_URL: API_URL },
      reuseExistingServer: false,
      timeout: 300_000,
    },
  ],
});
