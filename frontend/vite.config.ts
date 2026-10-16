import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
// =============================================================================
// [MODIFIED 2026-10-16]: Align Vite proxy with backend port 8744
// ORIGINAL INTENT: Hardcoded to port 8742 which was the default port before .env ALPHADESK_PORT=8744.
// REASON FOR CHANGE: Port 8742 was dead; backend is running on 8744, causing 500 ECONNREFUSED in UI.
// SAFE ROLLBACK: Preserved below.
// -----------------------------------------------------------------------------
// [PREVIOUS CODE - PRESERVED]:
//    proxy: {
//      "/api": { target: "http://127.0.0.1:8742", changeOrigin: true },
//      "/ws": { target: "ws://127.0.0.1:8742", ws: true },
//    },
// -----------------------------------------------------------------------------
// [NEW CODE]:
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${process.env.VITE_BACKEND_PORT || process.env.ALPHADESK_PORT || "8744"}`,
        changeOrigin: true,
      },
      "/ws": {
        target: `ws://127.0.0.1:${process.env.VITE_BACKEND_PORT || process.env.ALPHADESK_PORT || "8744"}`,
        ws: true,
      },
    },
// =============================================================================
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});
