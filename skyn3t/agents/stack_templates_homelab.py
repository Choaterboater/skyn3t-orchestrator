"""Deterministic React/Node template generators for the homelab-style
dashboard tier.

Lazy-loaded by ``skyn3t.agents.stack_templates`` via ``_homelab_mod()``.
Each exported function returns the FULL body of one file in the
scaffold. Keeping them here (instead of inline in ``stack_templates``)
keeps the parent module readable and isolates the dashboard-tier
specifics from the generic stack catalog.

Contract:
- Functions take simple inputs (a ``services`` list or nothing).
- Functions return non-empty strings that never include the
  ``TODO[skyn3t]`` stub marker — those are reserved for the
  ``code generation failed`` fallback path in CodeAgent.
- Output is self-consistent: ``App.jsx`` imports ``useConfig`` and
  references the components produced by the other generators; the
  Express routes use the schema ``config-store.js`` writes.
"""

from __future__ import annotations

from typing import List, Optional

_TRUE_FALSE_FROM_ENV = "process.env.NODE_ENV !== 'production'"


def _safe_services(services: List[str]) -> List[str]:
    """Normalize service slugs into JS-safe identifiers (kebab-case)."""
    seen: set[str] = set()
    out: List[str] = []
    for s in services or []:
        slug = (s or "").strip().lower().replace(" ", "-").replace("_", "-")
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    if not out:
        out = ["sonarr", "radarr", "prowlarr", "qbittorrent", "emby", "sonos"]
    return out[:8]


# ---------------------------------------------------------------------
# Frontend: top-level App + styles
# ---------------------------------------------------------------------

def app_jsx(services: List[str]) -> str:
    slugs = _safe_services(services)
    rows = ",\n  ".join(f'"{s}"' for s in slugs)
    return f"""import {{ useState }} from 'react';
import useConfig from './hooks/useConfig.js';
import StatusPill from './components/StatusPill.jsx';
import KpiTile from './components/KpiTile.jsx';
import ActivityFeed from './components/ActivityFeed.jsx';
import CommandPalette from './components/CommandPalette.jsx';
import ServiceDetail from './components/ServiceDetail.jsx';
import SettingsModal from './components/SettingsModal.jsx';

const DEFAULT_SERVICES = [
  {rows}
];

export default function App() {{
  const {{ config, status, save, reload }} = useConfig();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [selected, setSelected] = useState(null);

  const services = (config?.services && config.services.length)
    ? config.services
    : DEFAULT_SERVICES.map((slug) => ({{ slug, name: slug, enabled: true }}));

  return (
    <div className="dashboard-shell">
      <header className="dashboard-header glass">
        <h1>Homelab</h1>
        <div className="header-actions">
          <button onClick={{() => setPaletteOpen(true)}}>⌘K</button>
          <button onClick={{() => setSettingsOpen(true)}}>Settings</button>
          <StatusPill status={{status}} />
        </div>
      </header>

      <main className="dashboard-grid">
        {{services.map((svc) => (
          <KpiTile
            key={{svc.slug}}
            slug={{svc.slug}}
            name={{svc.name}}
            enabled={{svc.enabled}}
            onClick={{() => setSelected(svc)}}
          />
        ))}}
      </main>

      <aside className="dashboard-sidebar glass">
        <ActivityFeed />
      </aside>

      {{selected && (
        <ServiceDetail
          service={{selected}}
          onClose={{() => setSelected(null)}}
        />
      )}}

      {{paletteOpen && (
        <CommandPalette
          services={{services}}
          onClose={{() => setPaletteOpen(false)}}
          onSelect={{(svc) => {{ setSelected(svc); setPaletteOpen(false); }}}}
        />
      )}}

      {{settingsOpen && (
        <SettingsModal
          config={{config}}
          onSave={{save}}
          onReload={{reload}}
          onClose={{() => setSettingsOpen(false)}}
        />
      )}}
    </div>
  );
}}
"""


_DEFAULT_PALETTE: dict = {
    "bg":      "#0b0f1a",
    "bg-elev": "#121826",
    "text":    "#e6edf7",
    "muted":   "#8a93a8",
    "accent":  "#6ea8ff",
    "danger":  "#ff6b6b",
    "ok":      "#4ade80",
    "halo":    "#1a2336",  # gradient stop
}


def _hex_luminance(hex_str: str) -> float:
    """0..1 perceived luminance. Used to pick which slot each palette hex fills."""
    s = (hex_str or "").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) < 6:
        return 0.5
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return 0.5
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _resolve_palette(palette_hexes: Optional[List[str]]) -> dict:
    """Map a list of raw hex colors into the named slots styles.css needs.

    Without this, every canary shipped the hardcoded slate-blue palette
    regardless of what DesignerAgent picked — ContractVerifier flagged
    the same palette_schism on the same 8 hexes every single run, and
    the RAG-recorded lessons were useless (the source was a constant,
    not an LLM choice).

    Strategy: sort by luminance, pick darkest for bg, second-darkest for
    bg-elev, lightest for text, mid for muted, then a saturated mid for
    accent. Falls back to default when palette is empty or missing.
    """
    if not palette_hexes:
        return dict(_DEFAULT_PALETTE)
    by_lum = sorted(palette_hexes, key=_hex_luminance)
    out = dict(_DEFAULT_PALETTE)
    out["bg"]      = by_lum[0]
    out["bg-elev"] = by_lum[1] if len(by_lum) > 1 else by_lum[0]
    out["halo"]    = by_lum[2] if len(by_lum) > 2 else by_lum[-1]
    out["text"]    = by_lum[-1]
    # muted = a hex roughly two steps from text
    mid = max(0, len(by_lum) - 2)
    out["muted"]   = by_lum[mid]
    # accent = first non-extreme hex (preserves brand color when available)
    if len(by_lum) > 2:
        out["accent"] = by_lum[len(by_lum) // 2]
    return out


def _rgba_from_hex(hex_str: str, alpha: float) -> str:
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return f"rgba(110, 168, 255, {alpha})"
    return f"rgba({r}, {g}, {b}, {alpha})"


def styles_css(palette_hexes: Optional[List[str]] = None) -> str:
    """Return styles.css. When ``palette_hexes`` is given, the brand colors
    from palette.json are woven into the locked-shape stylesheet so the
    output matches what DesignerAgent picked. Backward-compatible: zero
    args returns the default slate palette so existing callers keep
    working.
    """
    p = _resolve_palette(palette_hexes)
    accent_soft = _rgba_from_hex(p["accent"], 0.12)
    return (
        ":root {\n"
        "  color-scheme: dark;\n"
        f"  --bg: {p['bg']};\n"
        f"  --bg-elev: {p['bg-elev']};\n"
        f"  --text: {p['text']};\n"
        f"  --muted: {p['muted']};\n"
        f"  --accent: {p['accent']};\n"
        f"  --accent-soft: {accent_soft};\n"
        f"  --danger: {p['danger']};\n"
        f"  --ok: {p['ok']};\n"
        "  --radius: 14px;\n"
        "  --blur: blur(18px);\n"
        "}\n"
        "\n"
        "* { box-sizing: border-box; }\n"
        "\n"
        "html, body, #root {\n"
        "  margin: 0;\n"
        "  padding: 0;\n"
        "  min-height: 100%;\n"
        f"  background: radial-gradient(circle at 20% -10%, {p['halo']} 0%, var(--bg) 60%);\n"
        "  color: var(--text);\n"
        "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;\n"
        "}"
    ) + """

.glass {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.08);
  backdrop-filter: var(--blur);
  -webkit-backdrop-filter: var(--blur);
  border-radius: var(--radius);
}

.dashboard-shell {
  display: grid;
  grid-template-columns: 1fr 320px;
  grid-template-rows: auto 1fr;
  gap: 16px;
  padding: 16px;
  min-height: 100vh;
}

.dashboard-header {
  grid-column: 1 / -1;
  padding: 14px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.dashboard-header h1 {
  margin: 0;
  font-size: 18px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.header-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}

.header-actions button {
  background: var(--accent-soft);
  color: var(--text);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  align-content: start;
}

.dashboard-sidebar {
  padding: 14px;
  overflow: auto;
}

.kpi-tile {
  padding: 16px;
  cursor: pointer;
  transition: transform 0.15s ease;
}

.kpi-tile:hover { transform: translateY(-1px); }

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--accent-soft);
}

.status-pill.ok    { color: var(--ok); }
.status-pill.error { color: var(--danger); }

.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}

.modal-card {
  width: min(640px, 92vw);
  max-height: 80vh;
  padding: 20px;
  overflow: auto;
}
"""


# ---------------------------------------------------------------------
# Frontend: hooks
# ---------------------------------------------------------------------

def use_config_js() -> str:
    return """import { useCallback, useEffect, useState } from 'react';

const CONFIG_URL = '/api/config';

export default function useConfig() {
  const [config, setConfig] = useState(null);
  const [status, setStatus] = useState('loading');

  const reload = useCallback(async () => {
    try {
      const res = await fetch(CONFIG_URL);
      if (!res.ok) throw new Error(`config load failed: ${res.status}`);
      const body = await res.json();
      setConfig(body);
      setStatus('ok');
    } catch (err) {
      console.error(err);
      setStatus('error');
    }
  }, []);

  const save = useCallback(async (next) => {
    setStatus('saving');
    try {
      const res = await fetch(CONFIG_URL, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error(`config save failed: ${res.status}`);
      const body = await res.json();
      setConfig(body);
      setStatus('ok');
      return true;
    } catch (err) {
      console.error(err);
      setStatus('error');
      return false;
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  return { config, status, save, reload };
}
"""


def use_polling_hook() -> str:
    return """import { useEffect, useRef, useState } from 'react';

export default function usePolling(fetcher, intervalMs = 5000, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const cancelled = useRef(false);

  useEffect(() => {
    cancelled.current = false;
    let timer;
    const tick = async () => {
      try {
        const next = await fetcher();
        if (!cancelled.current) setData(next);
      } catch (err) {
        if (!cancelled.current) setError(err);
      } finally {
        if (!cancelled.current) timer = setTimeout(tick, intervalMs);
      }
    };
    tick();
    return () => {
      cancelled.current = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error };
}
"""


# ---------------------------------------------------------------------
# Frontend: components
# ---------------------------------------------------------------------

def command_palette() -> str:
    return """import { useEffect, useMemo, useState } from 'react';

export default function CommandPalette({ services = [], onClose, onSelect }) {
  const [q, setQ] = useState('');

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return services;
    return services.filter((s) =>
      s.slug.toLowerCase().includes(needle) ||
      (s.name || '').toLowerCase().includes(needle)
    );
  }, [q, services]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card glass" onClick={(e) => e.stopPropagation()}>
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Jump to service"
        />
        <ul>
          {results.map((svc) => (
            <li key={svc.slug}>
              <button onClick={() => onSelect?.(svc)}>{svc.name || svc.slug}</button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
"""


def service_detail() -> str:
    return """import usePolling from '../hooks/usePolling.js';

export default function ServiceDetail({ service, onClose }) {
  const { data, error } = usePolling(async () => {
    const res = await fetch(`/api/services/${service.slug}/status`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    return res.json();
  }, 5000, [service.slug]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card glass" onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>{service.name || service.slug}</h2>
          <button onClick={onClose}>Close</button>
        </header>
        {error && <p className="error">Status check failed.</p>}
        {data && <pre>{JSON.stringify(data, null, 2)}</pre>}
      </div>
    </div>
  );
}
"""


def activity_feed() -> str:
    return """import usePolling from '../hooks/usePolling.js';

export default function ActivityFeed() {
  const { data, error } = usePolling(async () => {
    const res = await fetch('/api/activity');
    if (!res.ok) throw new Error(`activity ${res.status}`);
    return res.json();
  }, 8000, []);

  if (error) return <p className="error">Activity unavailable.</p>;
  const events = (data && data.events) || [];

  return (
    <section>
      <h3>Recent activity</h3>
      <ol>
        {events.map((e) => (
          <li key={e.id}>
            <time>{e.timestamp}</time>
            <span>{e.message}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
"""


def settings_modal_jsx() -> str:
    return """import { useState } from 'react';
import ServiceEditor from './ServiceEditor.jsx';

export default function SettingsModal({ config, onSave, onReload, onClose }) {
  const [draft, setDraft] = useState(() => ({
    services: (config && config.services) || [],
  }));

  const updateService = (slug, patch) => {
    setDraft((prev) => ({
      ...prev,
      services: prev.services.map((s) => (s.slug === slug ? { ...s, ...patch } : s)),
    }));
  };

  const persist = async () => {
    const ok = await onSave(draft);
    if (ok) onClose?.();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card glass" onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>Service profiles</h2>
          <button onClick={onReload}>Reload</button>
        </header>
        <p className="muted">
          Edit a service profile, test its connection, then save to persist
          the change to the server-side config store.
        </p>
        {draft.services.map((svc) => (
          <ServiceEditor
            key={svc.slug}
            service={svc}
            onChange={(patch) => updateService(svc.slug, patch)}
          />
        ))}
        <footer>
          <button onClick={onClose}>Cancel</button>
          <button onClick={persist}>Save</button>
        </footer>
      </div>
    </div>
  );
}
"""


def service_editor_jsx() -> str:
    return """import { useState } from 'react';

export default function ServiceEditor({ service, onChange }) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await fetch(`/api/config/${service.slug}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          base_url: service.base_url || '',
          api_key: service.api_key || '',
        }),
      });
      const body = await res.json().catch(() => ({}));
      setTestResult({ ok: res.ok && body.ok !== false, detail: body.detail || body.error || '' });
    } catch (err) {
      setTestResult({ ok: false, detail: String(err) });
    } finally {
      setTesting(false);
    }
  };

  return (
    <fieldset>
      <legend>{service.name || service.slug}</legend>
      <label>
        Base URL
        <input
          value={service.base_url || ''}
          onChange={(e) => onChange({ base_url: e.target.value })}
        />
      </label>
      <label>
        API key
        <input
          type="password"
          value={service.api_key || ''}
          onChange={(e) => onChange({ api_key: e.target.value })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={!!service.enabled}
          onChange={(e) => onChange({ enabled: e.target.checked })}
        />
        Enabled
      </label>
      <button type="button" onClick={runTest} disabled={testing}>
        {testing ? 'Testing…' : 'Test connection'}
      </button>
      {testResult && (
        <p className={testResult.ok ? 'ok' : 'error'}>
          {testResult.ok ? 'OK' : 'Failed'} {testResult.detail}
        </p>
      )}
    </fieldset>
  );
}
"""


# ---------------------------------------------------------------------
# Backend: Express server, config store, routes
# ---------------------------------------------------------------------

def config_store_js(services: List[str]) -> str:
    slugs = _safe_services(services)
    seed = ",\n    ".join(
        f'{{ slug: "{s}", name: "{s}", enabled: true, base_url: "", api_key: "" }}'
        for s in slugs
    )
    return f"""import fs from 'node:fs/promises';
import path from 'node:path';

const DATA_DIR = process.env.CONFIG_DIR || path.resolve(process.cwd(), 'server', 'data');
const CONFIG_FILE = path.join(DATA_DIR, 'user-config.json');

const DEFAULT_CONFIG = {{
  services: [
    {seed}
  ],
}};

async function ensureDataDir() {{
  await fs.mkdir(DATA_DIR, {{ recursive: true }});
}}

export async function load() {{
  await ensureDataDir();
  try {{
    const raw = await fs.readFile(CONFIG_FILE, 'utf8');
    return JSON.parse(raw);
  }} catch (err) {{
    if (err && err.code === 'ENOENT') {{
      await save(DEFAULT_CONFIG);
      return DEFAULT_CONFIG;
    }}
    throw err;
  }}
}}

export async function save(next) {{
  await ensureDataDir();
  const payload = JSON.stringify(next, null, 2);
  const tmp = `${{CONFIG_FILE}}.tmp`;
  await fs.writeFile(tmp, payload, 'utf8');
  await fs.rename(tmp, CONFIG_FILE);
  return next;
}}

export async function patchService(slug, patch) {{
  const current = await load();
  const services = (current.services || []).map((s) =>
    s.slug === slug ? {{ ...s, ...patch }} : s
  );
  return save({{ ...current, services }});
}}

export const DEFAULTS = DEFAULT_CONFIG;
"""


def server_index_js() -> str:
    return """import express from 'express';
import configRoute from './routes/config.js';

export async function createApp() {
  const app = express();
  app.use(express.json({ limit: '256kb' }));

  app.use('/api/config', configRoute);

  app.get('/api/health', (_req, res) => {
    res.json({ ok: true, uptime: process.uptime() });
  });

  app.get('/api/activity', (_req, res) => {
    res.json({ events: [] });
  });

  app.use((err, _req, res, _next) => {
    console.error('unhandled error', err);
    res.status(500).json({ error: 'internal error' });
  });

  return app;
}

export async function start(port = Number(process.env.PORT) || 4000) {
  const app = await createApp();
  return new Promise((resolve) => {
    const server = app.listen(port, () => {
      console.log(`server listening on :${port}`);
      resolve(server);
    });
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  start().catch((err) => {
    console.error('server failed to start', err);
    process.exit(1);
  });
}
"""


def config_route_js() -> str:
    return """import { Router } from 'express';
import { load, save, patchService } from '../config-store.js';

const router = Router();

router.get('/', async (_req, res, next) => {
  try {
    res.json(await load());
  } catch (err) { next(err); }
});

router.put('/', async (req, res, next) => {
  try {
    const next_ = req.body;
    if (!next_ || typeof next_ !== 'object') {
      res.status(400).json({ error: 'body must be a config object' });
      return;
    }
    res.json(await save(next_));
  } catch (err) { next(err); }
});

router.patch('/:slug', async (req, res, next) => {
  try {
    const slug = req.params.slug;
    const patch = req.body || {};
    res.json(await patchService(slug, patch));
  } catch (err) { next(err); }
});

router.post('/:slug/test', async (req, res) => {
  const { base_url, api_key } = req.body || {};
  if (!base_url) {
    res.status(400).json({ ok: false, detail: 'base_url required' });
    return;
  }
  try {
    const headers = api_key ? { 'X-Api-Key': api_key } : {};
    const probe = await fetch(`${base_url.replace(/\\/$/, '')}/api/system/status`, { headers });
    res.json({ ok: probe.ok, status: probe.status });
  } catch (err) {
    res.json({ ok: false, detail: String(err && err.message ? err.message : err) });
  }
});

export default router;
"""
