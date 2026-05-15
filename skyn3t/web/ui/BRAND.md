# SkyN3t — Brand Document

> One-line aesthetic summary: **Warm graphite atelier** — a dark, scholarly machine-room where ember light cuts through graphite shadow; every pixel feels like it has been running for a decade.

---

## 1. Inferred Aesthetic Intent

The user asked for a homelab dashboard (Sonarr, Radarr, qBittorrent, Emby, Sonos, Docker). The implicit mood is not cyberpunk neon, not sterile SaaS minimalism, and not a NOC console. It is the aesthetic of a **server closet that has been humming in the dark for years**: warm, lived-in, slightly industrial, trustworthy. The palette therefore leans into deep gunmetal browns, warm cream text, and a single ember-orange accent that signals "heat" and "activity" without shouting. This is the "Tactical Ops (warm)" machine-room recipe from the designer skill, adapted to the exact hex values supplied.

---

## 2. Palette

All tokens use the exact hex codes from the brief.

| Token | Hex | Usage |
|-------|-----|-------|
| `bg-0` | `#0F0D0A` | Page canvas — deep graphite brown, almost black |
| `bg-1` | `#1A1714` | Sidebar, elevated rails |
| `bg-2` | `#24201A` | Cards, panels — the primary surface |
| `bg-3` | `#2E2920` | Inputs, table headers, code blocks — one step above cards |
| `border` | `#3B3429` | Default hairline borders |
| `border-strong` | `#4A4337` | Hover/active borders |
| `text-primary` | `#E8DDCB` | Headings, primary text — warm cream |
| `text-secondary` | `#8C8270` | Body text, labels — muted stone |
| `text-dim` | `#5C5448` | Placeholders, disabled metadata |
| `accent` | `#E05C1A` | **Ember orange** — active states, progress fills, links, sparklines |
| `accent-strong` | `#F06E2E` | Hover accent — slightly brighter ember |
| `accent-soft` | `rgba(224, 92, 26, 0.08)` | Subtle highlight backgrounds |
| `accent-line` | `rgba(224, 92, 26, 0.22)` | Active nav borders, focus rings |
| `status-green` | `#7A9E6A` | Online, success — desaturated so it doesn't compete with accent |
| `status-yellow` | `#C49A3A` | Warning, pending — warm amber, not traffic-light yellow |
| `status-red` | `#B05040` | Error, offline — brick red, not alarm red |

### Background Effects

- **Base:** `#0F0D0A` solid graphite
- **Bloom gradients** (pseudo-elements, `pointer-events: none`):
  - Top-left: warm ember ellipse `rgba(224, 92, 26, 0.08)` at 10% -10%
  - Bottom-right: deep brown ellipse `rgba(40, 28, 20, 0.55)` at 100% 110%
- **Dot grain overlay:** `radial-gradient(circle at 1px 1px, rgba(224, 92, 26, 0.03) 1px, transparent 0)` at 22px grid, 50% opacity, `mix-blend-mode: overlay`

---

## 3. Typography

| Role | Font | Weight | Usage |
|------|------|--------|-------|
| **Display / H1** | **Orbitron** | 500 (Medium) | Page titles, hero numbers — geometric, engineered, slightly sci-fi without being gimmicky |
| **Sans / Body** | **Rajdhani** | 400 (Regular), 500 (Medium) | UI labels, body text, nav — narrow, technical, reads well at 13px |
| **Mono / Data** | **JetBrains Mono** | 400 | Numbers, timestamps, code blocks, paths, API keys — tabular-nums everywhere |

### Scale

| Size | Value | Usage |
|------|-------|-------|
| Micro | `0.6rem` (~9.6px) | Status pills, micro labels |
| Small | `0.75rem` (12px) | Section labels, table headers, secondary info |
| Body | `0.875rem` (14px) | Standard UI text |
| Large | `1.125rem` (18px) | Sub-headings, stat values |
| Display | `2.25rem` (36px) | Page titles (Orbitron) |

### Conventions

- **Tabular numerals** on every numeric value: `font-variant-numeric: tabular-nums`
- **Uppercase labels:** `text-xs uppercase tracking-wider text-text-secondary` for table headers, card titles, form labels
- **Display headings:** Orbitron, weight 500, letter-spacing 0.02em, line-height 1.1
- **Monospace data:** JetBrains Mono on IDs, timestamps, numeric values, code blocks
- **No italic** — the atelier's scholarly italic is replaced by Orbitron's engineered geometry

---

## 4. Voice

Three adjectives that govern every piece of UI copy, empty state, and error message:

1. **Competent** — The dashboard knows what is happening and says so plainly. No "Oops!" or "Something went wrong." Use "qBittorrent connection lost" or "3 services offline."
2. **Understated** — No exclamation marks, no celebratory animations. Success is a green dot and the word "online." Failure is a red dot and the word "offline."
3. **Warm** — Despite the density and precision, the language is human. "Good evening, stephen" not "USER: stephen | SESSION: active." The warmth comes from the palette and tone, not from emojis or fluff.

---

## 5. Logo Concepts

Three distinct concepts, each tied to the brief's homelab + machine-room aesthetic:

### Concept A: The Ember Node
A single hexagon (nod to Docker/container honeycomb) with a small ember-orange glow at one vertex, the rest in graphite line-art. The glow pulses subtly in the live UI. Clean enough for a favicon, readable at 16px. Evokes: "one node in a warm, running cluster."

### Concept B: The Rack Glyph
Three horizontal bars of decreasing width, stacked like a server rack or a signal-strength icon. The top bar is ember-orange (the "active" rack), the lower two are graphite. A single vertical line on the left unifies them, like a rack rail. Evokes: "hardware that is on and doing work."

### Concept C: The N3t Mark
A stylized "N3" where the "3" is drawn as three concentric arcs (like Wi-Fi / signal waves) emanating from the vertical stroke of the "N". The arcs fade from ember-orange at the source to graphite at the edge. The "t" is a simple crossbar, small, tucked to the right. Evokes: "network + intelligence + warmth."

---

## 6. Component Quick-Reference

### Cards
```
bg-bg-2 border border-border rounded-lg p-3/4/5
```

### Status Pills
```
inline-block px-2 py-0.5 rounded text-[0.6rem] uppercase tracking-wider border
```
| State | Classes |
|-------|---------|
| Online | `bg-status-green/10 border-status-green/30 text-status-green` |
| Warning | `bg-status-yellow/10 border-status-yellow/30 text-status-yellow` |
| Offline | `bg-status-red/10 border-status-red/30 text-status-red` |
| Neutral | `bg-bg-3 border-border text-text-secondary` |

### Buttons
| Type | Classes |
|------|---------|
| Primary | `bg-accent text-bg-0 hover:bg-accent-strong` |
| Secondary | `bg-bg-3 border border-border hover:border-border-strong` |
| Ghost | `text-text-dim hover:text-text-primary` |
| Danger | `bg-status-red text-bg-0` |

### Inputs
```
w-full bg-bg-3 border border-border rounded px-2 py-1.5 text-sm font-mono outline-none focus:border-accent
```

---

*Derived from the Tactical Ops (warm) machine-room palette recipe, adapted to the exact hex values supplied in the brief.*
