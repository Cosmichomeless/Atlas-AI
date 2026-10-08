import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // La imagen de Docker compila con `NEXT_OUTPUT=standalone` (servidor mínimo sin `node_modules`);
  // el resto de los usos (`next dev`, `next start`, E2E) no cambian.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
};

export default nextConfig;
