---
name: designer-dashboard-density
tags: [designer, dashboard, ui-pattern, density]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# Dashboard density done right

Dashboards exist to surface dozens of values at once. The aesthetic
mistake is treating them like consumer apps with generous whitespace.
The right move is **dense, scannable, monospace-anchored** UI that
respects the user's intelligence.

## Concrete patterns

- **11-13px body type, line-height 1.45.** Inter and similar grotesks
  hold up. SF Pro Text also works. Avoid Roboto at small sizes.
- **`tabular-nums` on all numeric values.** Without it, 99% and 100%
  jitter as the bar updates. CSS: `font-variant-numeric: tabular-nums`
  on every metric, kv-value, timestamp.
- **Data tables, not cards, for >5 rows.** Cards waste screen real
  estate. Use cards for "now playing" / "running container" tiles
  (rich, mixed content). Use rows for "queue items" / "download list"
  (uniform, scannable).
- **One accent color, used surgically.** Pick one (#00D4FF cyan,
  #E05C1A ember, etc.) and use it for: active states, progress fills,
  one row of icons. Never for body text. Never on more than ~5% of
  visible pixels.
- **Status via color + symbol.** Green dot + "online", red triangle +
  "offline". Never color-only — fails colorblind tests AND screen
  readers.

## Anti-patterns to avoid

- ❌ Gradient backgrounds on cards (kills text legibility)
- ❌ Rounded corners larger than 8px on data containers (consumer-app feel)
- ❌ Shadow + border + gradient on the same card (too many lifts)
- ❌ Emoji as status indicators (reads as toy)
- ❌ Animation on data updates beyond a 100ms fade

## Reference targets

- Homarr (good information density, weak hierarchy)
- Vercel dashboard (excellent type scale and color discipline)
- Grafana (the canonical example of "dashboard tools should look like tools")
- Linear (the consumer-app crossover; works because they care about every pixel)

## Quick palette recipes

**Cool ops**: `#0A0E1A` bg / `#1A1F2E` panel / `#00D4FF` accent / `#E2E8F0` text

**Warm hardware**: `#0F0D0A` bg / `#1F1A14` panel / `#E05C1A` accent / `#E8DDCB` text

**Mid-century console**: `#10131A` bg / `#1E232E` panel / `#A0DA9C` accent / `#D8E0EA` text
