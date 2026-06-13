---
name: designer-anti-slop-banned-patterns
description: Use when generating any UI/frontend output — avoid the mechanical AI tells that reviewers and the anti-slop gate reject.
tags: [designer, anti-slop, quality, code, consistency_reviewer]
triggers: [ui, frontend, design, html, jsx, css]
---

# Avoid AI-slop patterns in generated UI

Before shipping any HTML/JSX/CSS, eliminate these mechanical tells (the
`anti_slop` static gate flags them as warnings that feed skill grading):

- **No placeholder content** in shipped output: never `Jane Doe`, `John Doe`,
  `Acme Inc`, `Lorem ipsum`, `example@example.com`, `your-name-here`. Use real,
  brief-specific names and data.
- **No em-dashes in copy.** Em-dashes (—) in user-facing text are a classic AI
  tell — use commas, periods, or parentheses instead.
- **No overused AI-default display fonts** (Fraunces, Playfair Display) unless
  the brief explicitly asks for them. Pick type that fits the actual product.
- **No raw scroll listeners** (`addEventListener('scroll', …)`) for reveal or
  parallax effects — they jank on cheap devices. Use `IntersectionObserver`.
- **No three-identical-card hero rows** used as filler. Vary the content, or cut
  the section.

Match the brief's actual domain and aesthetic. Generic, templated UI gets
rejected — the point of a design pass is that it does NOT look auto-generated.
