import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev-mode: the SPA on :5173 reaches the Observatory backend on :8765
    // through this proxy, so `api.ts` can use a same-origin relative base in
    // both dev and the FastAPI static-mount deployment.
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/events": "http://127.0.0.1:8765",
    },
  },
});
