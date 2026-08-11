/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev-mode: the SPA on :5173 reaches the Observatory backend on :8765
    // through this proxy, so `api.ts` can use a same-origin relative base in
    // both dev and the FastAPI static-mount deployment.
    //
    // `historyApiFallback` is not set and is not needed: Vite's dev server
    // already serves `index.html` for unmatched navigation requests, which is
    // what makes `/p/proj/r/run/grouping` refreshable in dev. In production the
    // backend's own catch-all (`observatory/app.py:_mount_spa`) does the same.
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/events": "http://127.0.0.1:8765",
    },
  },
  // `npm test` — vitest, no watcher, non-zero on failure. See `docs/observatory.md`.
  //
  // Four surfaces rot first and every one of them fails silently in a browser:
  // the route shape (a tab that stops being reachable by URL still renders
  // *something*), the status map (a state added upstream renders blank), the
  // stage-scrubbing helpers (a stage list that stops matching the trace), and
  // `PathChip` (a chip that copies its own ellipsis). All four fail loudly here.
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
