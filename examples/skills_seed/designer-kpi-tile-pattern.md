---
name: designer-kpi-tile-pattern
tags: [designer, code_agent, dashboard, ui-pattern, react, kpi]
success_count: 1
failure_count: 0
last_used_at: 1778600000.0
source: hand-curated:seed-skill
created_at: 1778600000.0
---

# KPI tile — for the top-of-dashboard summary strip

A KPI tile is the small, glanceable number+label box that lives in
the topbar or above the service grid. Homarr, Linear, Vercel, every
modern dashboard uses these to give a 2-second status. They are NOT
the same shape as service cards — they're denser and label-first.

## Anatomy

- **Width**: 140–200px each, in a horizontal row of 3–6 tiles
- **Padding**: 12px
- **Border**: 1px var(--border), 8px radius
- **Background**: var(--panel)
- **Internal layout** (single column):
  - TOP: 11px uppercase label in var(--text-dim), letter-spacing 0.05em
    Examples: `SERVICES ONLINE`, `TOTAL BANDWIDTH`, `STORAGE USED`,
    `ACTIVE ALERTS`, `UPTIME`
  - MIDDLE: large value, 22–26px, font-weight 600, var(--text)
    With unit at smaller size: `13` `Services` or `3.2` `TB`
    Tabular numerals.
  - BOTTOM: optional 10px context line — delta arrow + change vs
    last period, or status pill. Examples: `↑ 3% from last week`,
    `2 incidents 30-Day Uptime`, `Requires attention`

## Color rules

- The label and label-color stay neutral
- The value color depends on health:
  - Good (e.g. uptime 99.9%): var(--text)
  - Warning (e.g. bandwidth nearing cap, 1 alert pending): amber `#F59E0B`
  - Critical (e.g. service offline, disk >90%): red `#EF4444`
- A subtle 1px LEFT BORDER in the brand/health color gives the row
  a sense of "live status" without being noisy

## Layout pattern (React + CSS)

```jsx
<div className="kpi-row">
  <KpiTile label="Services Online" value="13" unit="Services" tone="ok" />
  <KpiTile label="Total Bandwidth" value="3.2" unit="TB" tone="ok" />
  <KpiTile label="Network" value="↑847" unit="Mbps" tone="ok" />
  <KpiTile label="Active Alerts" value="1" unit="Pending" tone="warn" />
</div>
```

```css
.kpi-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }
.kpi { background: var(--panel); border: 1px solid var(--border);
       border-radius: 8px; padding: 12px; border-left: 2px solid var(--tone-color); }
.kpi .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
               color: var(--text-dim); margin-bottom: 4px; }
.kpi .value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.kpi .unit  { font-size: 13px; color: var(--text-dim); margin-left: 4px; }
.kpi .delta { font-size: 10px; color: var(--text-dim); margin-top: 4px; }
```

## What NOT to do

- Don't put icons in KPI tiles — they compete with the value
- Don't animate the number on every poll — it makes the eye twitch
- Don't put more than 6 tiles in a row — past that it's noise
- Don't use these for per-service stats — that's the service-card row
