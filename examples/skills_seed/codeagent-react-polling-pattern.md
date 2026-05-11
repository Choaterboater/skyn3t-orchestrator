---
name: codeagent-react-polling-pattern
tags: [code_agent, react_vite, react, polling, integration]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# Polling pattern for live-data React components

For dashboard widgets that pull live data from a backend (Sonarr
queue, Docker containers, Emby sessions, etc.), use this exact
pattern. It handles abort-on-unmount, interval drift, and error
state correctly.

## The hook

```jsx
import { useEffect, useState, useRef } from 'react';

function usePolling(fetchFn, intervalMs = 5000) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const fetchRef = useRef(fetchFn);
  fetchRef.current = fetchFn;

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const ctrl = new AbortController();

    const refetch = async (signal) => {
      try {
        const result = await fetchRef.current(signal);
        if (!cancelled) {
          setData(result);
          setError(null);
          setLastUpdated(Date.now());
        }
      } catch (e) {
        if (e.name !== 'AbortError' && !cancelled) {
          setError(e);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    const tick = async () => {
      if (cancelled) return;
      await refetch(ctrl.signal);
      if (!cancelled) timer = setTimeout(tick, intervalMs);
    };
    tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      ctrl.abort();
    };
  }, [intervalMs]);

  return { data, error, loading, lastUpdated };
}
```

## Usage

```jsx
function DownloadQueue() {
  const { data, error, loading } = usePolling(
    (signal) => fetch('/api/queue', { signal }).then(r => r.json()),
    5000,
  );
  if (loading && !data) return <div className="loading-shimmer" />;
  if (error) return <div className="error">{error.message}</div>;
  return data.items.map(item => <DownloadRow key={item.id} {...item} />);
}
```

## Why this pattern

- **AbortController on every fetch.** Without it, unmounting mid-fetch
  leaks the response into setState → "can't update state on unmounted
  component" warning.
- **`setTimeout` chained, not `setInterval`.** setInterval fires even
  when the previous request is still running, causing pileup. The
  chained-timeout pattern only schedules the next tick AFTER the
  current one completes.
- **fetchFn captured in ref.** Avoids re-running the effect every
  render just because the parent passed a new arrow function.
- **`cancelled` flag for late returns.** AbortController doesn't
  prevent the `.then` chain from running if the request already
  resolved — the flag does.

## Intervals worth using

- 4-5s: download queues, currently-playing media, Sonos transport
- 10-15s: container stats, service health, indexer status
- 30s+: configuration, schemas, anything that rarely changes
- never: long-poll instead, or WebSocket for true streaming
