---
name: designer-status-pill-pattern
tags: [designer, code_agent, dashboard, ui-pattern, react, status, accessibility]
success_count: 1
failure_count: 0
last_used_at: 1778600000.0
source: hand-curated:seed-skill
created_at: 1778600000.0
---

# Status pills — the universal "is it working" indicator

Every service card, every alert row, every connection-test result
uses the same status pill. Having ONE pill shape and ONE convention
saves dozens of inconsistent variants the LLM would otherwise invent.

## Shape

`{6px dot} {LABEL}` inside a rounded-full pill:
- padding: 2px 8px
- border-radius: 9999px (fully rounded)
- font-size: 10px
- text-transform: uppercase
- letter-spacing: 0.05em
- font-weight: 500
- inline-flex, gap: 6px, align-items: center

## States (each has dot color, text color, bg color)

| State | Dot | Text/border | Background |
|---|---|---|---|
| Online / Running / OK | `#10B981` (emerald) | same | `rgba(16,185,129,0.12)` |
| Warning / Degraded | `#F59E0B` (amber) | same | `rgba(245,158,11,0.12)` |
| Offline / Stopped / Error | `#EF4444` (red) | same | `rgba(239,68,68,0.12)` |
| Pending / Loading | `#6B7280` (gray) | same | `rgba(107,114,128,0.12)` |
| Active stream / Just-now | `#3B82F6` (blue) | same | `rgba(59,130,246,0.12)` |

The dot is a 6×6 circle, no border. NEVER use the dot color as the
text color directly — it's hard to read. Use the dot color at 100%
for text/border IF the bg is the same color at 12%. The bg lifts
the text enough for AA contrast.

## Accessibility — never color-only

Every pill MUST pair the color with a label AND the dot symbol shape.
Don't ever ship "color-only" status. Reasons:
- Colorblind users (~8% of male users)
- Greyscale screenshots
- Dashboard at-a-glance from across a room

## React + CSS

```jsx
function StatusPill({ tone = 'ok', children }) {
  return <span className={`pill pill-${tone}`}>
    <span className="dot" />
    {children}
  </span>;
}
```

```css
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 2px 8px; border-radius: 9999px;
  font-size: 10px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.pill .dot { width: 6px; height: 6px; border-radius: 50%; }
.pill-ok      { color: #10B981; background: rgba(16,185,129,0.12); }
.pill-ok .dot { background: #10B981; }
.pill-warn    { color: #F59E0B; background: rgba(245,158,11,0.12); }
.pill-warn .dot { background: #F59E0B; }
.pill-err     { color: #EF4444; background: rgba(239,68,68,0.12); }
.pill-err .dot { background: #EF4444; }
.pill-info    { color: #3B82F6; background: rgba(59,130,246,0.12); }
.pill-info .dot { background: #3B82F6; }
```

## What NOT to do

- Don't make the pill a button. If clicking does something, that's
  a separate icon button next to the pill.
- Don't shrink the dot below 5px or grow above 8px. The 6px sweet
  spot reads from across a room without dominating the pill text.
- Don't put icons inside the pill (just dot + label). Icons compete
  with the dot.
