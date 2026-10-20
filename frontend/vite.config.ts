import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // Read the same .env values as the backend launcher. A clean checkout uses
  // the backend default (8742), while a local override can still select 8744
  // or any explicitly configured VITE_BACKEND_PORT.
  const env = loadEnv(mode, process.cwd(), "");
  const backendPort = process.env.VITE_BACKEND_PORT || env.VITE_BACKEND_PORT || env.ALPHADESK_PORT || "8742";

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
