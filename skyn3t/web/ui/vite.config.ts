import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite config for the SkyN3t SPA.
//
// Dev: `npm run dev` serves on :5173 with HMR. /api/* and /ws* are
// proxied to the FastAPI backend at :6660 so cookies/auth flow
// naturally — no separate CORS config needed in dev.
//
// Build: `npm run build` emits to dist/. FastAPI's web/app.py can
// optionally mount that dir at /static and serve the SPA shell from
// index.html. The old dashboard.html stays as a fallback until we
// reach feature parity.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Backend's _http_origin_allowed compares Origin against the
      // request's netloc — in dev, the browser sends Origin:
      // http://localhost:5173 but the request lands on 127.0.0.1:6660,
      // so non-GETs (POST/PATCH/DELETE) get 401 "Cross-origin browser
      // access denied." Stripping Origin makes the backend treat it
      // as a same-origin call, which is correct: dev proxy is a
      // trusted loopback hop.
      "/api": {
        target: "http://127.0.0.1:6660",
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:6660" },
      },
      "/ws": { target: "ws://127.0.0.1:6660", ws: true, changeOrigin: true },
      "/traces": {
        target: "http://127.0.0.1:6660",
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:6660" },
      },
      "/webhooks": {
        target: "http://127.0.0.1:6660",
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:6660" },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
