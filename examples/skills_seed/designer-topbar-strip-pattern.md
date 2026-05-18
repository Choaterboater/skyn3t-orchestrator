---
name: designer-topbar-strip-pattern
tags: [designer, code_agent, dashboard, ui-pattern, react, topbar, layout]
success_count: 1
failure_count: 0
last_used_at: 1778600000.0
source: hand-curated:seed-skill
created_at: 1778600000.0
---

# Top-of-dashboard strip — greeting, KPIs, search

The first thing a user sees on a homelab dashboard. Homarr nails
this with "Good afternoon, Alex" + a row of 4 KPI tiles + a global
search box. Heimdall has a more spartan version. Either way, the
TOP STRIP gives instant situational awareness.

## Layout

A horizontal strip across the top of the dashboard, ABOVE the
service grid. Height ~80–110px total.

```
+----------------------------------------------------------------+
| Good afternoon, Alex.        [13 Services] [3.2 TB] [↑847 Mbps]|
|                              [1 Alert Pending]                 |
+----------------------------------------------------------------+
|                  [ Search services, devices, ... ⌘K ]          |
+----------------------------------------------------------------+
```

## Composition

1. **Greeting line** (left side, large)
   - 28–32px, font-weight 600
   - Time-of-day aware: `Good morning, X.` / `Good afternoon, X.` / `Good evening, X.`
   - User name comes from a config value (default `there` or no name)
2. **KPI strip** (right side of greeting line, OR below it)
   - 3–5 KPI tiles in a horizontal row. See `designer-kpi-tile-pattern`.
3. **Global search** (full width, below the greeting row)
   - Big input — height 44px, full width minus 200px on either side
   - Placeholder `Search services, devices, settings...`
   - Right side shows `⌘K` shortcut hint (gray pill)
   - Opens a command palette (Cmd+K from anywhere on page also opens)
4. **Optional secondary action row** below search
   - Quick actions: `Add Service`, `Refresh All`, `Wake On LAN`, etc.
   - Compact pill buttons, icon + label

## What good vs bad looks like

GOOD topbar:
- Greeting is human ("Good afternoon" not "DASHBOARD")
- KPIs show the 4 things you most want to know at a glance
- Search is FULL-WIDTH and visually prominent
- Big keyboard shortcut hint

BAD topbar (what the LLM defaults to):
- Just an `<h1>Homelab</h1>` and a settings gear in the corner
- No KPIs
- Search shoved into a tiny corner OR missing entirely

## React skeleton

```jsx
function Topbar() {
  const hour = new Date().getHours();
  const tod = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
  const userName = useConfig()?.config?.userName || 'there';
  return (
    <header className="topbar">
      <div className="topbar-row">
        <h1 className="greeting">Good {tod}, {userName}.</h1>
        <KpiStrip />
      </div>
      <SearchBox shortcut="⌘K" />
    </header>
  );
}
```

## Cmd+K command palette

- Wire `window.addEventListener('keydown', e => { if ((e.metaKey||e.ctrlKey) && e.key==='k') openPalette() })`
- Palette is a centered overlay (different from the right-drawer pattern)
- Filters across: services, devices (Sonos zones, lights), settings sections, recent activity
- Each result is `{icon} {name} {category-pill}` — click to open or jump
