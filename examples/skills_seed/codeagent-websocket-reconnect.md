---
name: codeagent-websocket-reconnect
tags: [code_agent, react_vite, react, websocket, integration]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# WebSocket reconnect with exponential backoff

Browsers don't reconnect WebSockets for you. If the backend restarts,
your stream is dead until the page reloads. This pattern handles
reconnect cleanly, including StrictMode double-mount in dev.

## The hook

```jsx
import { useEffect, useRef, useState } from 'react';

function useWebSocket(url, onMessage) {
  const [status, setStatus] = useState('connecting');
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    let cancelled = false;
    let retry = 0;
    let timer = null;
    let ws = null;

    const connect = () => {
      if (cancelled) return;
      try {
        ws = new WebSocket(url);
      } catch {
        return scheduleReconnect();
      }

      ws.onopen = () => {
        if (cancelled) { ws.close(); return; }
        retry = 0;
        setStatus('live');
      };
      ws.onclose = () => {
        if (cancelled) return;
        setStatus('offline');
        scheduleReconnect();
      };
      ws.onerror = () => {
        if (cancelled) return;
        setStatus('offline');
      };
      ws.onmessage = (e) => {
        if (cancelled) return;
        try {
          handlerRef.current(JSON.parse(e.data));
        } catch { /* malformed */ }
      };
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay = Math.min(30000, 2000 * 2 ** retry);
      retry += 1;
      timer = setTimeout(connect, delay);
    };

    connect();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (ws) {
        // Detach handlers BEFORE close — onclose firing after cleanup
        // schedules reconnects past unmount, infinite-loop in StrictMode.
        ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
        try { ws.close(); } catch {}
      }
    };
  }, [url]);

  return status;
}
```

## Why this exact shape

- **`cancelled` flag checked in every callback.** StrictMode mounts →
  unmounts → re-mounts. Without the flag, the dying socket's `onclose`
  schedules a reconnect AFTER cleanup ran.
- **Detach handlers in cleanup.** Otherwise the unmounted socket
  still fires its event handlers as it shuts down.
- **Exponential backoff starts at 2s, caps at 30s.** Starts higher
  than 1s so a StrictMode double-mount doesn't immediately reconnect.
- **`handlerRef` for the message callback.** Avoids re-mounting the
  effect every time the parent passes a new arrow function.

## Usage

```jsx
function ActivityStream() {
  const [events, setEvents] = useState([]);
  const status = useWebSocket('/ws/swarm', (msg) => {
    setEvents(prev => [msg, ...prev].slice(0, 500));
  });
  return (
    <>
      <span className={`badge ${status}`}>{status}</span>
      <ul>{events.map((e, i) => <li key={i}>{e.label}</li>)}</ul>
    </>
  );
}
```

## Status badge styling

```css
.badge.live    { background: rgba(74,222,128,0.2); color: #4ade80; }
.badge.live::before { content: '●'; animation: pulse 2s infinite; }
.badge.offline { background: rgba(248,113,113,0.2); color: #f87171; }
.badge.connecting { background: rgba(148,163,184,0.2); color: #94a3b8; }
```
