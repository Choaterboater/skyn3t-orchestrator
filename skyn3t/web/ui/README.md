# SkyN3t UI — fresh build

A real Vite + React + Tailwind SPA over the FastAPI backend.

This is the primary SkyN3t UI. The legacy `skyn3t/web/dashboard.html` has been removed; FastAPI serves the built SPA from `skyn3t/web/ui/dist/index.html` at `/`.

## Quick start

```bash
cd skyn3t/web/ui
npm install
npm run dev
```

That starts Vite on `http://localhost:5173`. The dev server proxies `/api/*`, `/ws*`, `/traces`, `/webhooks/*` through to the FastAPI backend at `http://127.0.0.1:6660`, so you need both running.

## Build

```bash
npm run build
```

Outputs to `skyn3t/web/ui/dist/`. The FastAPI app can mount this dir at `/static` and serve `index.html` for the SPA shell once the rebuild reaches parity.

## What's here so far

- **Overview** — three live tiles (status, agents, build patterns).
- **Agents** — registered-agent table with proper truncation + status pills.
- **Chat** — Claude-desktop-style chat against any registered agent.
- **Skills** — durable skill library viewer, tag-filterable.
- **Build Patterns** — per-stack scoreboard with click-to-expand details.

## Design tokens

The palette (`#0f0e0c` graphite + `#c9a96e` amber accent) matches the atelier tokens already in the backend's `:root` so the SPA and the legacy dashboard look like the same product.

Typography: Instrument Serif (display italic) + Space Grotesk (UI) + JetBrains Mono (code).
