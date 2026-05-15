---
name: designer-detail-drawer-pattern
tags: [designer, code_agent, dashboard, ui-pattern, react, drawer]
success_count: 1
failure_count: 0
last_used_at: 1778600000.0
source: hand-curated:seed-skill
created_at: 1778600000.0
---

# Service detail drawer — slide-in panel for per-service deep dive

Clicking a service card on a homelab dashboard SHOULD slide in a
detail drawer from the right, showing per-service stats + recent
items + actions. This is what Homarr/Heimdall do for Plex (shows
recently-added + continue-watching), Sonarr (missing + upcoming),
qBittorrent (torrent details).

Right-side drawer beats:
- Modal (steals focus, locks the page)
- Inline expand (shoves grid around)
- Separate page (loses dashboard context)

## Anatomy

- **Position**: fixed, right: 0, top: 0, bottom: 0
- **Width**: 480px on desktop, 100vw on mobile
- **Background**: var(--panel), with backdrop overlay 60% black behind it
- **Animation**: slide from translateX(100%) → 0 in 180ms ease-out
- **Padding**: 24px
- **Close**: Esc key, click outside, X button top-right

## Layout (top to bottom)

1. **Header row**
   - LEFT: 32×32 service icon + service name (18px font-weight 600)
   - SAME ROW: status pill
   - RIGHT: "Open <service>" deep-link button + close X button
2. **Stats grid** (4-column)
   - Same KPI tile pattern as the topbar KPIs, but service-specific:
     - Plex: `4 LIBRARIES`, `12,431 MOVIES`, `847 TV EPISODES`, `3 ACTIVE STREAMS`
     - Sonarr: `24 SERIES MONITORED`, `1 MISSING`, `150 COMPLETE`
     - qBittorrent: `8 ACTIVE`, `78.4 MB/s ↓`, `12.1 MB/s ↑`, `1.42 RATIO`
   - 4 across on desktop, 2 across on narrow
3. **Tabs or sections** (depends on service)
   - Sonarr: `Missing` / `Add Show` / `Upcoming` tabs
   - Plex: `Recently Added` grid + `Continue Watching` list
   - qBittorrent: `Active` / `Seeding` / `Completed` tabs
4. **Recent items list / grid**
   - Plex poster grid: 3 columns of 100×140 art tiles with title + year
     + star rating beneath
   - Sonarr missing list: icon + show name + S0XE0Y aired date + a
     small `Search` button on the right of each row
   - Continue-watching list: play icon + title + horizontal progress
     bar + remaining time
5. **Footer actions**
   - Service-specific buttons: `Scan Library`, `Settings`, `Open Web UI`
   - Pinned to bottom; primary button uses brand color

## React shell

```jsx
function ServiceDrawer({ slug, service, onClose }) {
  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose(); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  if (!slug) return null;
  return createPortal(
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">
        <DrawerHeader service={service} onClose={onClose} />
        <ServiceStats slug={slug} service={service} />
        <ServiceBody slug={slug} service={service} />
        <ServiceFooter slug={slug} service={service} />
      </aside>
    </>,
    document.body,
  );
}
```

## What NOT to do

- Don't load the entire service's history at open — fetch in chunks
  as the user scrolls or switches tabs.
- Don't trap focus inside the drawer in a way that breaks Esc and
  outside-click. Both must close it.
- Don't put the drawer over the topbar — leave the topbar visible
  (top: 56px) so the user knows they're still on the dashboard.
- Don't make the drawer modal-style centered. It's a SIDE drawer.

## Wiring with the card grid

- Card click (anywhere except action-row icons) → open drawer
- Drawer takes `slug` + the `service` config object as props
- One drawer instance at the top level of App.jsx, not one per card
