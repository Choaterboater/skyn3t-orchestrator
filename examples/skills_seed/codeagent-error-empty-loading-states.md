---
name: codeagent-error-empty-loading-states
tags: [code_agent, react_vite, react, ux, integration]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# Loading, empty, and error states are part of the integration

The most common way real-integration code looks fake is by missing
loading/empty/error rendering. When the API works → it looks great.
When the API is down → it looks broken because it shows a static
"No data" message that's indistinguishable from "the integration was
never wired."

## The three states every component needs

```jsx
function ServiceWidget({ fetchUrl }) {
  const { data, error, loading, lastUpdated } = usePolling(
    (signal) => fetch(fetchUrl, { signal }).then(r => r.json()),
    5000,
  );

  // 1. LOADING — first fetch hasn't returned yet
  if (loading && !data) {
    return <SkeletonCard />;
  }

  // 2. ERROR — fetch failed (network, 5xx, 4xx)
  if (error) {
    return (
      <ErrorCard
        title="Couldn't reach service"
        detail={error.message}
        // Show how to fix it, not just that it broke
        hint="Check that the service is running and the env vars are set."
      />
    );
  }

  // 3. EMPTY — fetch succeeded but data is genuinely empty
  if (data && Array.isArray(data.items) && data.items.length === 0) {
    return (
      <EmptyCard
        title="Nothing here yet"
        // Distinguish from error — this is a healthy state
        detail={`Last checked ${fmtRelative(lastUpdated)}`}
      />
    );
  }

  // 4. NORMAL
  return <DataTable items={data.items} />;
}
```

## The styles

```css
.skeleton-card {
  background: rgba(30, 41, 59, 0.75);
  height: 5rem;
  position: relative;
  overflow: hidden;
}
.skeleton-card::after {
  content: '';
  position: absolute;
  inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(90deg,
    transparent,
    rgba(226, 232, 240, 0.14),
    transparent
  );
  animation: shimmer 1.25s infinite;
}
@keyframes shimmer { 100% { transform: translateX(100%); } }

.error-card {
  border-left: 2px solid var(--err);
  background: rgba(248, 113, 113, 0.06);
  padding: 0.85rem;
}

.empty-card {
  border: 1px dashed var(--border);
  padding: 1.5rem;
  text-align: center;
  color: var(--text-dim);
}
```

## Anti-patterns

- ❌ Showing nothing during the loading state ("eventually something
  appears"). Users assume the page is broken.
- ❌ Treating empty and error the same. "No data" with a red border is
  misleading. "Server unreachable" with a dashed border is misleading.
- ❌ Error states that don't say what to do. "Failed" is useless.
  "Sonarr at http://localhost:8989 returned 401 — is SONARR_API_KEY
  set?" is useful.
- ❌ Long-lived loading state with a generic spinner. After ~10s,
  show a stale-but-cached version with a warning that the latest
  fetch is taking too long.

## Last-updated stamps

Every live-data widget should show when the data was last
successfully refreshed. Helps users distinguish "stale because the
poll just failed" from "fresh, this is just how the data looks now."

```jsx
<span className="last-updated">
  {lastUpdated
    ? `Updated ${fmtRelative(lastUpdated)}`
    : 'Updating…'}
</span>
```
