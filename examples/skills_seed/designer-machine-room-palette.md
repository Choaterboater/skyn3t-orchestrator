---
name: designer-machine-room-palette
tags: [designer, palette, dashboard, dark-mode]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# Machine-room palette recipes

Five dark-mode palette recipes battle-tested for dashboards, ops
consoles, and "things that run 24/7" UIs. Pick one, don't mix.

## 1. Tactical Ops (warm)

The visual language of a command terminal that's been on for a decade.

```
--bg:        #0F0D0A    /* canvas */
--panel:     #1F1A14    /* card surfaces */
--panel-2:   #16140F    /* depth layer */
--accent:    #E05C1A    /* ember orange — active states, progress */
--text:      #E8DDCB    /* warm cream */
--text-dim:  #8C8270    /* muted labels */
--border:    #2A2419    /* hairlines */
```

When: SkyN3t homelab dashboard, hardware monitoring, anything where
"server room heat" is the metaphor.

## 2. Cool Ops

The Vercel / Linear aesthetic — engineered, precise, slightly cold.

```
--bg:        #0A0E1A
--panel:     #1A1F2E
--panel-2:   #0D1117
--accent:    #00D4FF    /* cyan — feels electrical */
--text:      #E2E8F0
--text-dim:  #94A3B8
--border:    rgba(148,163,184,0.25)
```

When: developer tools, infrastructure dashboards, analytics.

## 3. Console Green

The original. Phosphor monitor + dark room.

```
--bg:        #0A0F0A
--panel:     #131A13
--panel-2:   #1A2419
--accent:    #4ADE80    /* phosphor green */
--text:      #D4E4D4
--text-dim:  #6B8A6B
--border:    #1F2A1F
```

When: log viewers, terminal-like tools, anything that wants to feel like 1985.

## 4. Mid-century Console

Cool palette with mint accent — Braun product design meets Grafana.

```
--bg:        #10131A
--panel:     #1E232E
--panel-2:   #161A22
--accent:    #A0DA9C    /* dust mint */
--text:      #D8E0EA
--text-dim:  #7A8493
--border:    #2A2F3A
```

When: data analytics, scientific instruments, anything where "calm"
beats "exciting."

## 5. Carbon (Neutral)

When you don't want the palette itself to have personality.

```
--bg:        #0D0D0D
--panel:     #1A1A1A
--panel-2:   #131313
--accent:    #FFFFFF    /* yes, white as accent — used sparingly */
--text:      #E8E8E8
--text-dim:  #888888
--border:    #2A2A2A
```

When: photography portfolios, music players, anywhere the content is
the point.

## Universal rules across all five

- **Status semantics use named colors, not the palette accent:**
  - `--ok: #4ADE80` (green)
  - `--warn: #FBBF24` (amber)
  - `--err: #F87171` (red)
- **Accent appears on ≤5% of visible pixels.** If it's everywhere it's
  nowhere.
- **Three depth levels, not five.** `bg → panel → panel-2`. Anything
  deeper and the eye loses the hierarchy.
- **Border colors are ALWAYS lower contrast than text.** If your
  border is more visible than your label, swap them.

## Skip these palette mistakes

- ❌ Pure black (#000) as bg with pure white (#FFF) as text. Too much
  contrast, induces eye strain on long sessions.
- ❌ Multiple accent colors ("brand has a yellow AND a blue!"). One.
- ❌ Saturated accent at 100%. Drop saturation to 70-80% for sustained
  viewing — `#00D4FF` is fine for 30s, fatiguing at 8 hours.
- ❌ Gradient accents on small UI elements (buttons, badges). Solid
  beats gradient at small sizes.
