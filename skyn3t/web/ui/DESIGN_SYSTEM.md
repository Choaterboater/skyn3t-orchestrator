# SkyN3t Web UI — Design System Audit

> Compiled from full codebase grep analysis of `src/**/*.tsx` and `src/**/*.ts`.

---

## 1. Color Palette

### Semantic Tokens (from `tailwind.config.js`)

| Token | Hex | Usage |
|-------|-----|-------|
| `bg-0` | `#0f0e0c` | Page background (atelier base) |
| `bg-1` | `#16140f` | Sidebar, elevated surfaces |
| `bg-2` | `#1d1a13` | **Cards, panels** (most common) |
| `bg-3` | `#25211a` | **Table headers, inputs, code blocks** |
| `border` | `#2d2920` | Default borders |
| `border-strong` | `#3b362a` | Hover/active borders |
| `text-primary` | `#f4ede0` | Headings, primary text |
| `text-secondary` | `#b8ad96` | **Body text, labels** (most frequent) |
| `text-dim` | `#7a705f` | Placeholders, muted metadata |
| `accent` | `#c9a96e` | **Primary accent** — links, active states, icons |
| `accent-strong` | `#d9b97e` | Hover accent |
| `accent-soft` | `rgba(201,169,110,0.08)` | Subtle highlight backgrounds |
| `accent-line` | `rgba(201,169,110,0.22)` | Active nav borders |
| `status-green` | `#8aa37a` | Success, online |
| `status-yellow` | `#d4a64a` | Warning, pending |
| `status-red` | `#b65a4a` | Error, offline, failed |

### Usage Frequency (descending)

**Text:** `text-secondary` (64) → `text-sm` (54) → `text-dim` (41) → `text-accent` (38) → `text-xs` (27) → `text-status-red` (15) → `text-primary` (10) → `text-status-green` (8) → `text-status-yellow` (5)

**Background:** `bg-bg-3` (46) → `bg-bg-2` (42) → `bg-accent-soft` (17) → `bg-status-red` (16) → `bg-bg-0` (9) → `bg-status-green` (8) → `bg-status-yellow` (4) → `bg-bg-1` (1)

**Border:** `border-border` (60) → `border-accent` (20) → `border-accent-line` (15) → `border-status-red` (14) → `border-status-green` (8) → `border-strong` (7) → `border-status-yellow` (4)

---

## 2. Typography

### Font Stack

| Role | Font | Fallback |
|------|------|----------|
| Display (H1, hero) | **Instrument Serif** | Georgia, serif |
| Sans (UI, body) | **Space Grotesk** | system-ui, sans-serif |
| Mono (data, code) | **JetBrains Mono** | ui-monospace, monospace |

### Scale & Patterns

| Size | Tailwind | Usage |
|------|----------|-------|
| `text-[0.6rem]` | Custom | **Status pills, micro labels** |
| `text-[0.65rem]` | Custom | Agent metadata, timestamps |
| `text-xs` | 0.75rem | Section labels, table headers, secondary info |
| `text-sm` | 0.875rem | **Body text** (most common) |
| `text-base` | 1rem | Standard UI text |
| `text-lg` | 1.125rem | Sub-headings |
| `text-4xl` | 2.25rem | Display headings (Instrument Serif italic) |

### Text Styling Conventions

- **Uppercase labels:** `text-xs uppercase tracking-wider text-text-secondary` — used for table headers, card titles, form labels
- **Display headings:** `.display` class → italic, weight 400, letter-spacing -0.01em, line-height 1.05
- **Monospace data:** `font-mono` on IDs, timestamps, numeric values, code blocks (78 occurrences)
- **Label pattern:** `text-xs uppercase tracking-wider text-text-secondary font-medium mb-2`

---

## 3. Spacing & Layout

### Spacing Scale (most used)

| Token | Count | Usage |
|-------|-------|-------|
| `p-2` / `px-2 py-1.5` | 29 | Inline elements, compact rows |
| `p-3` / `px-3 py-2` | 26 | **Standard card padding** |
| `p-4` | 18 | Spacious cards, forms |
| `p-5` | 6 | Large panels, empty states |
| `space-y-3` | 11 | Card internal stacking |
| `space-y-6` | 9 | Page sections |
| `gap-3` | — | Grid gaps (Studio uses `gap-5` for major columns) |

### Layout Patterns

| Pattern | Usage |
|---------|-------|
| `flex` | 59 occurrences — **dominant layout mode** |
| `grid` | 13 occurrences — page-level columns, card grids |
| `block` | 17 occurrences — labels, wrappers |
| `grid-cols-[260px_minmax(0,1fr)]` | App shell: sidebar + main |
| `grid-cols-[300px_minmax(0,1fr)]` | Studio: file tree + editor |
| `grid-cols-[minmax(0,1fr)_380px]` | Traces: list + detail |
| `grid-cols-2` | Two-column forms |

### Critical Layout Utilities

| Utility | Count | Purpose |
|---------|-------|---------|
| `min-w-0` | 20 | **Prevents flex/grid children from overflowing** |
| `truncate` | 30 | Ellipsis overflow for long text |
| `overflow-hidden` | 2 | Card clipping |
| `shrink-0` | — | Prevents sidebar/buttons from squishing |

---

## 4. Component Patterns

### Cards

```
rounded-lg border border-border bg-bg-2 p-3/4/5
```

- Always `rounded-lg` (8px) — never sharp or fully rounded
- Always 1px `border-border` — subtle separation
- Background is always `bg-bg-2` or `bg-bg-3`
- Padding varies by density: `p-3` (compact), `p-4` (standard), `p-5` (spacious)

### Tables

```
<thead className="bg-bg-3 text-text-secondary text-xs uppercase tracking-wider">
<tbody>
  <tr className="border-t border-border">
    <td className="px-3 py-2">...</td>
  </tr>
</tbody>
```

- Header: `bg-bg-3` with uppercase tracking-wider labels
- Rows: `border-t border-border` separators
- Cells: `px-3 py-2` standard padding
- Empty state: `colSpan` with centered `text-text-dim`

### Status Pills

```
inline-block px-2 py-0.5 rounded text-[0.6rem] uppercase tracking-wider border
```

| State | Classes |
|-------|---------|
| Error | `bg-status-red/10 border-status-red/30 text-status-red` |
| Success | `bg-status-green/10 border-status-green/30 text-status-green` |
| Warning | `bg-status-yellow/10 border-status-yellow/30 text-status-yellow` |
| Neutral | `bg-bg-3 border-border text-text-secondary` |

- All use **20% opacity background** + **30% opacity border** + solid text
- Font: `text-[0.6rem]` (micro) uppercase with `tracking-wider`

### Forms / Inputs

```
<input className="w-full bg-bg-3 border border-border rounded px-2 py-1.5
  text-sm font-mono outline-none focus:border-accent" />
```

- Background: `bg-bg-3` (darker than cards)
- Border: `border-border` default, `focus:border-accent` on focus
- No focus ring — border color shift only
- Monospace for code/config inputs; sans for regular text

### Buttons

| Type | Classes |
|------|---------|
| Primary | `bg-accent text-bg-0 hover:bg-accent-strong` |
| Secondary | `bg-bg-3 border border-border hover:border-border-strong` |
| Ghost | `text-text-dim hover:text-text-primary` |
| Danger | `bg-status-red text-bg-0` |

### Navigation (Sidebar)

- Active: `bg-accent-soft text-accent-strong border-r-2 border-accent`
- Inactive: `text-text-secondary hover:bg-accent-soft hover:text-text-primary`
- Container: `bg-bg-1 border-r border-border` (260px fixed)

---

## 5. Background Effects

The `.bg-atelier` class (applied to root) creates the signature look:

1. **Base:** `#0f0e0c` solid graphite
2. **Bloom gradients** (pseudo-elements, `pointer-events: none`):
   - Top-left: warm amber ellipse `rgba(201, 169, 110, 0.10)` at 10% -10%
   - Bottom-right: deep brown ellipse `rgba(58, 38, 28, 0.55)` at 100% 110%
3. **Dot grain overlay:** `radial-gradient(circle at 1px 1px, rgba(201, 169, 110, 0.04) 1px, transparent 0)` at 22px grid, 50% opacity, `mix-blend-mode: overlay`

---

## 6. Hover & Interaction Patterns

| Pattern | Count | Usage |
|---------|-------|-------|
| `hover:text-text-primary` | 7 | Links, nav items |
| `hover:border-border-strong` | 7 | Cards, buttons |
| `hover:bg-bg-3` | 4 | List rows |
| `hover:bg-accent-soft` | 1 | Nav highlight |
| `hover:bg-status-red` | 3 | Danger actions |
| `hover:bg-status-green` | 1 | Success actions |
| `hover:underline` | 2 | Text links |

**No transitions** are defined — the UI is instant/snappy.

---

## 7. Z-Index & Layering

| Layer | Z-Index | Element |
|-------|---------|---------|
| Background effects | 0 | `.bg-atelier::before/::after` |
| Content | 1+ | Normal flow |
| Modals / overlays | — | Not currently used |

---

## 8. Design System Summary

> **Aesthetic:** Warm graphite atelier — dark, scholarly, understated luxury.

| Property | Value |
|----------|-------|
| **Base hue** | Warm amber/brown (≈30°) |
| **Contrast ratio** | Low-to-moderate (relaxed, not harsh) |
| **Border radius** | Small and consistent: `rounded` (4px) for pills/inputs, `rounded-lg` (8px) for cards |
| **Shadows** | **None** — depth via borders and background layers only |
| **Transitions** | **None** — instant state changes |
| **Density** | Medium-tight; `space-y-3` inside cards, `gap-5` between major sections |
| **Information hierarchy** | Size + color + uppercase tracking for labels; monospace for data |
| **Signature elements** | Instrument Serif italic headings, amber accent on graphite, dot grain texture |

---

## 9. Files Analyzed

```
src/App.tsx
src/routes/OverviewPage.tsx
src/routes/AgentsPage.tsx
src/routes/StudioPage.tsx
src/routes/CortexPage.tsx
src/routes/ActivityPage.tsx
src/routes/TracesPage.tsx
src/routes/SkillsPage.tsx
src/routes/KnowledgePage.tsx
src/routes/BuildPatternsPage.tsx
src/routes/ChatPage.tsx
src/api/client.ts
src/styles/globals.css
tailwind.config.js
```

---

*Audit generated from automated grep analysis — counts reflect exact occurrences across all TSX/TS files.*
