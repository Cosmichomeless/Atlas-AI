import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import { configDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [tsconfigPaths(), react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    // Los specs de Playwright viven en `e2e/` y se ejecutan con `npm run e2e`.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
