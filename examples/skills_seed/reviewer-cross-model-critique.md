---
name: reviewer-cross-model-critique
tags: [reviewer, critique, quality, integration]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# How to critique another agent's output

The job of a cross-model critic is NOT to rewrite the previous
agent's work. It's to surface the 1-3 issues that, if left alone,
will make the next stage's work worse. Be brutal about what matters,
quiet about everything else.

## What to flag

- **Hallucinated APIs** — endpoints/headers/response fields that
  don't exist or differ from the real product. Especially for things
  named in research.md.
- **Missing real integration** — components that hardcode demo arrays
  instead of fetching. Look for `const FAKE_DATA = [...]`, hardcoded
  ports, hardcoded zone names, hardcoded usernames.
- **Broken imports** — `import App from './App.jsx'` when no `App.jsx`
  is in the file plan. `import { useState } from 'react'` but no
  React in package.json.
- **Inconsistent design language** — accent color used 30 times in
  one component, none in the next. Spacing scale that drifts.
- **Missing loading/error/empty states** for components that fetch.
- **Architecture mismatches** — backend says "Express proxy on 3001",
  frontend hardcodes `fetch('http://localhost:8989/...')` directly.

## What to NOT flag

- **Style preferences** that don't break anything. "I'd have used
  reduce here instead of forEach" — silently let it through.
- **Optimization opportunities** that don't affect correctness.
  Premature optimization is not a critique.
- **Choices the brief already made.** If brief says "Vite + React",
  don't suggest switching to Next.
- **The agent's tone or wording.** You're reviewing OUTPUT, not the
  agent's writing voice.

## Output format

Exactly one of:
1. `NO_ISSUES` (verbatim, nothing else) — when the output is solid
2. Up to 3 numbered lines: `<file/section>: <problem> → <fix>`

Examples of good critique lines:

```
1. src/App.jsx:42 → fetches /api/v3/queue directly with no auth header;
   architecture.md says Express proxy on port 3001 handles auth → call
   /api/queue (proxy path) instead.

2. src/DownloadList.jsx → DOWNLOADS const is a hardcoded array of fake
   items, no fetch() anywhere → wire to usePolling hook reading from
   /api/queue per the integration spec.

3. package.json → missing 'dotenv' and 'cors' which architecture.md's
   proxy implementation requires → add as dependencies.
```

Examples of bad critique lines (don't do these):

```
1. The code could be more modular. ← vague, not actionable
2. I would have written this differently. ← preference, not a defect
3. Consider using TypeScript instead. ← not in the brief's scope
```

## When in doubt

If you're not sure whether something is an issue: imagine you're
shipping this code to production tomorrow. Will the issue cause a
runtime error, a security problem, or a visible UX failure? If yes,
flag it. If no, let it pass.

The reviewer's bar is "would I sign off on this?", not "could this be
better?". Everything could be better.
