import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

/**
 * Shared by dev and preview, deliberately one object.
 *
 * The UI calls `/api/*` relative to its own origin — `VITE_API_BASE` is only
 * set for deployed builds — so whatever serves the page has to forward that
 * prefix to the local API. `server.proxy` does NOT carry over to
 * `vite preview`, which takes its own `preview.proxy`; splitting them let the
 * built-output workflow fall back to a plain static file server, which answers
 * every `/api` call with its own 404 while the backend sits idle.
 *
 * Backend: python lambdas/api/local_server.py 8787
 */
const serve = {
  allowedHosts: ["host.docker.internal"], // MCP/containerized browsers in dev
  proxy: {
    "/api": "http://127.0.0.1:8787",
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: serve,
  preview: serve,
});
