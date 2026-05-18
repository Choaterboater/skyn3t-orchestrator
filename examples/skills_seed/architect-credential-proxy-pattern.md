---
name: architect-credential-proxy-pattern
tags: [architect, system-design, integration, security, backend]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# Credential-holding proxy pattern

When a frontend needs to talk to multiple third-party services that
each require their own API key (Sonarr, Radarr, Stripe, Slack, GitHub
API, etc.), **never put the credentials in the browser**. Build a
thin backend proxy that holds the keys server-side and exposes a
normalized API to the SPA.

## Why

- API keys in the browser bundle = anyone who opens devtools owns them
- CORS gets complicated when calling N different services directly
- Unix socket access (Docker, system metrics) requires a server-side
  process anyway
- Rate-limiting, caching, response normalization happen in one place

## The shape

```
Browser (React/Vue/Svelte SPA)
  ↓ fetch('/api/...')
Express / FastAPI / Hono proxy (port 3001)
  ↓ axios/httpx with API keys from env
External services (Sonarr, Radarr, Emby, Stripe, ...)
```

## Backend conventions (Express example)

```js
// proxies/sonarr.js
const SONARR_URL = process.env.SONARR_URL || 'http://localhost:8989';
const SONARR_API_KEY = process.env.SONARR_API_KEY;

export async function getSonarrQueue() {
  if (!SONARR_API_KEY) throw new Error('SONARR_API_KEY not set');
  const res = await fetch(`${SONARR_URL}/api/v3/queue?page=1&pageSize=50`, {
    headers: { 'X-Api-Key': SONARR_API_KEY },
  });
  if (!res.ok) throw new Error(`sonarr ${res.status}`);
  return res.json();
}

// server.js
app.get('/api/sonarr/queue', async (_req, res) => {
  try {
    res.json(await getSonarrQueue());
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});
```

## Normalization

The proxy is the right place to translate vendor-specific response
shapes into a unified domain model. Example: Sonarr returns
`records[].series.title`, Radarr returns `records[].movie.title`.
Normalize to `{ id, title, type: 'tv'|'movie', progress, eta }` so
the React side has one shape for both.

## What goes in env vars

```
SONARR_URL, SONARR_API_KEY
RADARR_URL, RADARR_API_KEY
EMBY_URL, EMBY_API_KEY
QBIT_URL, QBIT_USER, QBIT_PASS
PROWLARR_URL, PROWLARR_API_KEY
DOCKER_SOCKET=/var/run/docker.sock     # default fine
```

Document them in README. Use `dotenv` (Node) or `python-dotenv` (FastAPI).

## When to skip the proxy

- The service has CORS open AND auth via short-lived session cookie
  the user already has (rare for self-hosted)
- You're shipping a CLI or desktop app, not a browser SPA — but even
  then a thin local server is often cleaner than embedding secrets

## Deployment shape

Single Docker Compose stack:
- `app` service runs the proxy (also serves the SPA's built dist/)
- `sonarr`, `radarr`, etc. each own services pointing at their data volumes
- All services on the same Docker network so URLs are
  `http://sonarr:8989` not `localhost:8989`
- Browser only ever talks to the proxy on its published port
