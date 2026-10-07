import { vi } from "vitest";

/** Doble de `next/navigation`: `replace`/`push` quedan registrados y no navegan. */
export const router = { replace: vi.fn(), push: vi.fn(), back: vi.fn(), refresh: vi.fn() };

export function resetRouter() {
  for (const fn of Object.values(router)) fn.mockReset();
}
