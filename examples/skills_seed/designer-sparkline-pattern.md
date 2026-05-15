---
name: designer-sparkline-pattern
tags: [designer, code_agent, dashboard, ui-pattern, react, sparkline, recharts]
success_count: 1
failure_count: 0
last_used_at: 1778600000.0
source: hand-curated:seed-skill
created_at: 1778600000.0
---

# Sparklines — tiny inline time-series

A sparkline gives a card 10× more information density without any
extra labels. It's the most-underused widget in dashboard UIs. Every
service card on a homelab/status dashboard should have a sparkline
of a relevant 1-hour metric.

## When to use

| Service | Sparkline metric |
|---|---|
| Sonarr / Radarr | queue depth over the last hour |
| qBittorrent | download speed kB/s |
| Pi-hole | queries per minute |
| Emby / Plex / Jellyfin | active stream count |
| Docker host | aggregate CPU% |
| Home Assistant | events per minute |
| UniFi | total throughput |
| Tautulli | bandwidth |

## Implementation: hand-rolled SVG (no library needed)

A sparkline is just a `<polyline>` in an SVG. No recharts/d3 weight.
For a 60×24 sparkline of `points = [0..1]` normalized values:

```jsx
function Sparkline({ values, color = '#3b82f6', height = 24, width = 60 }) {
  if (!values || values.length < 2) {
    return <div className="sparkline-empty" style={{ height, width }} />;
  }
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const step = width / (values.length - 1);
  const points = values
    .map((v, i) => `${i * step},${height - ((v - min) / range) * height}`)
    .join(' ');
  return (
    <svg width={width} height={height} role="img" aria-label="trend">
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
```

## Implementation: recharts (when you ALSO need the big charts)

If the project already has recharts (analytics page), use it:

```jsx
import { LineChart, Line, ResponsiveContainer } from 'recharts';

<ResponsiveContainer width={60} height={24}>
  <LineChart data={values.map((v, i) => ({ i, v }))}>
    <Line type="monotone" dataKey="v" stroke={color} strokeWidth={1.5} dot={false} />
  </LineChart>
</ResponsiveContainer>
```

But **prefer the hand-rolled SVG for cards**. Recharts pulls in
ResponsiveContainer + animations that aren't needed at 60×24.

## Color rules

- Stroke uses the SERVICE BRAND COLOR (Sonarr cyan #35C5F0, qBit blue
  #406EBC, etc.) so the line is identifiable at a glance.
- 1.5px stroke. 1px is too thin on Retina, 2px+ feels heavy.
- No fill under the line for card sparklines (saves visual weight).
  In the big analytics chart, fill with the same color at 10% opacity.
- Empty state: a 1px dashed neutral line, not a flat solid one.

## Data source

- Each card's `usePolling(path, interval)` hook should keep the last
  ~60 samples (1h of 1-min polls) in a ring buffer kept in a ref or
  module-level cache.
- DON'T store in component state — re-renders on every poll cause
  layout thrash.
