import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `rifferr serve --dev` (API-13): Vite serves the UI and proxies API calls to the
// backend, which accepts the Vite origin only in --dev mode.
const backend = process.env.RIFFERR_BACKEND ?? "http://127.0.0.1:8765";
const proxied = { target: backend, changeOrigin: true };

export default defineConfig({
  plugins: [react()],
  server: {
    // Vite never binds beyond localhost.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": proxied, "/auth": proxied, "/healthz": proxied, "/openapi.json": proxied },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
