import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // =============================================================================
  // [MODIFIED 2026-11-11]: Fix Vite Dev Proxy Target Port & Root .env Discovery
  // ORIGINAL INTENT: Read ALPHADESK_PORT only from process.cwd() (frontend/) with fallback "8742".
  // REASON FOR CHANGE: Root repository .env defines ALPHADESK_PORT=8744. In frontend dev mode,
  // process.cwd() is "frontend", failing to locate ../.env and causing 500 ECONNREFUSED on /api & /ws.
  // SAFE ROLLBACK: The original code is preserved below as comments.
  // -----------------------------------------------------------------------------
  // [PREVIOUS CODE - PRESERVED]:
  // // Read the same .env values as the backend launcher. A clean checkout uses
  // // the backend default (8742), while a local override can still select 8744
  // // or any explicitly configured VITE_BACKEND_PORT.
  // const env = loadEnv(mode, process.cwd(), "");
  // const backendPort = process.env.VITE_BACKEND_PORT || env.VITE_BACKEND_PORT || env.ALPHADESK_PORT || "8742";
  // -----------------------------------------------------------------------------
  // [NEW CODE]:
  const rootEnv = loadEnv(mode, path.resolve(__dirname, ".."), "");
  const localEnv = loadEnv(mode, process.cwd(), "");
  const env = { ...rootEnv, ...localEnv };
  const backendPort = process.env.VITE_BACKEND_PORT || env.VITE_BACKEND_PORT || env.ALPHADESK_PORT || "8744";
  // =============================================================================

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${backendPort}`,
          changeOrigin: true,
        },
        "/ws": {
          target: `ws://127.0.0.1:${backendPort}`,
          ws: true,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: false,
      chunkSizeWarningLimit: 900,
    },
  };
});
