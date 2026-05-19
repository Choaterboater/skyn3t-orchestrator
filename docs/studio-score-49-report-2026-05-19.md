# Studio 49-Score Report

Date: 2026-05-19

This report covers the two most recent Studio project runs whose saved `review.md` scored **49/100**:

1. `build-a-website-for-inventory-lookup-able-to-upload-excel-fi-2d4498`
2. `build-a-habit-tracker-with-streaks-beea80`

Project roots:

- `~/Documents/Skyn3t/Projects/build-a-website-for-inventory-lookup-able-to-upload-excel-fi-2d4498`
- `~/Documents/Skyn3t/Projects/build-a-habit-tracker-with-streaks-beea80`

## Executive summary

Both projects landed on **49/100** for the same immediate reason:

- the reviewer's **LLM score was below 50**
- reviewer blending would have produced a higher number
- but the reviewer then **caps the final score at 49 whenever `llm_score < 50`**

That means these were not "barely 49" runs. They were **forced down to 49 by the reviewer bucket clamp** after the LLM judged them as `no-go`.

In both cases, the underlying project problems were also similar:

- docs, architecture, tech stack, and scaffold contradicted each other
- packaging implied a backend/fullstack story that the shipped code did not actually implement
- major designed components existed on paper or as isolated files, but the user-facing app did not actually use them

## Why they were exactly 49

Current reviewer logic:

- blend with LLM: `0.54 * llm + 0.36 * heuristic + 0.10 * packaging`
- then clamp to the LLM verdict bucket:
  - `llm < 50` -> final score cannot exceed `49`
  - `50 <= llm < 75` -> final score cannot exceed `74`

So once the LLM review decides the artifact is `no-go`, the final score is capped at **49** even if heuristic checks are very high.

## Project 1 — Inventory lookup website

Project:

- `build-a-website-for-inventory-lookup-able-to-upload-excel-fi-2d4498`

Saved review summary:

- Verdict: `no-go`
- Final score: `49/100`
- Breakdown: `heuristic=96`, `llm=42`, `packaging=7/10`

Unclamped blended score:

- `0.54*42 + 0.36*96 + 0.10*70 = 64.24`
- rounded score would have been **64**
- final score became **49** because `llm_score=42` triggered the reviewer clamp

### Main reasons it scored poorly

From the saved review:

1. **The shipped app and the written architecture disagree**
   - architecture promises Postgres/API/backend behavior
   - scaffold is basically a client-side React app
   - `tech_stack.json`, Docker, README, and runtime story all disagree

2. **Packaging tells a false backend story**
   - review calls out Python Docker packaging for a project that does not actually ship a Python backend
   - backend/fullstack expectations were applied, but the repo did not contain a real backend implementation

3. **Brand/design consistency collapsed**
   - palette tokens contradict themselves
   - dark/light direction conflicts between docs and implementation
   - tokens, logo, styles, and brand docs drift from each other

4. **Scaffold contains dead or misleading pieces**
   - `Settings.jsx` is effectively a stub
   - dead backend dependencies in `package.json`
   - wrong product title in `index.html`

### How to fix it

1. **Choose one real product shape**
   - either make it a real frontend-only app
   - or implement the backend the docs claim exists

2. **Remove fake backend/fullstack signaling if there is no backend**
   - remove unused `express` / `better-sqlite3` deps
   - stop claiming API/backend services in README and stack docs
   - do not ship backend Docker/compose files unless they run

3. **Make architecture, README, and scaffold match**
   - same ports
   - same runtime
   - same deployment story
   - same product title

4. **Fix the design system at the source**
   - normalize `brand.md`, `palette.json`, `tokens.json`, `tokens.css`, and actual CSS/classes
   - ensure semantic colors map to distinct visible values

5. **Delete or implement stubs**
   - `Settings.jsx` should either be real and useful or removed
   - same for any "future backend" claims

## Project 2 — Habit tracker with streaks

Project:

- `build-a-habit-tracker-with-streaks-beea80`

Saved review summary:

- Verdict: `no-go`
- Final score: `49/100`
- Breakdown: `heuristic=100`, `llm=29`, `packaging=7/10`

Unclamped blended score:

- `0.54*29 + 0.36*100 + 0.10*70 = 58.66`
- rounded score would have been **59**
- final score became **49** because `llm_score=29` triggered the reviewer clamp

### Main reasons it scored poorly

From the saved review:

1. **The actual app is not the app that was designed**
   - designed components exist
   - but the real entrypoint is a separate light-themed localStorage demo
   - the intended branded component system is not what users see

2. **Backend story is contradictory or missing**
   - architecture implies one backend
   - `tech_stack.json` implies another
   - Docker implies Python
   - actual repo appears to have no backend at all

3. **Critical feature promises are unimplemented**
   - no real auth flow
   - no reminder settings/UI
   - no working fullstack wiring despite README/deployment claims

4. **Core UI/design assets are incomplete**
   - `WeekStrip.jsx` stub
   - `Settings.jsx` dead code
   - Tailwind-based components without a working Tailwind toolchain
   - palette/token semantics are internally broken

### How to fix it

1. **Promote the intended UI to the real entrypoint**
   - wire `main.jsx` / `App.jsx` to the real branded component tree
   - delete the temporary localStorage demo once replaced

2. **Pick a single backend story and implement it**
   - if frontend-only: remove backend claims, auth claims, proxy config, Docker story
   - if fullstack: add the actual backend code, routes, runtime files, and working compose setup

3. **Make "fullstack" true before claiming it**
   - working backend files
   - consistent backend port
   - real auth UI
   - real reminder support if it is part of the brief

4. **Fix build-system mismatches**
   - if using Tailwind, install and configure it
   - if not using Tailwind, rewrite components to match the actual CSS pipeline

5. **Eliminate broken placeholder files**
   - implement `WeekStrip.jsx`
   - implement or remove `Settings.jsx`
   - remove stale titles and template leftovers

## Common root cause across both runs

These two 49s are not random. They share a repeat pattern:

1. High heuristic score because files exist and look "complete" structurally
2. Low LLM score because the artifact story breaks under semantic review
3. Reviewer clamp forces final score to 49 because the LLM verdict is still `no-go`

In plain terms:

- the swarm produced **a lot of files**
- but the files did **not tell one coherent truth**
- reviewer heuristics rewarded presence
- the LLM punished contradiction
- the bucket clamp made that contradiction visible as a hard **49**

## Fastest way to stop getting 49s

If the goal is to avoid the repeated `49/100` outcome, the highest-leverage fixes are:

1. **Do not ship contradictory stack stories**
   - docs, README, tech stack, ports, Docker, and actual code must agree

2. **Do not claim fullstack/backend unless the backend really exists**
   - fake backend scaffolding is heavily punished by the LLM reviewer

3. **Make the actual user entrypoint match the designed component system**
   - if the polished components are not imported/rendered, the review treats them as unused theater

4. **Delete stubs and template leftovers before review**
   - wrong title
   - dead settings page
   - stub components
   - unused backend deps

5. **Raise the LLM score above 50**
   - because once `llm_score < 50`, the reviewer will cap the final score at 49 anyway

## Recommended code-level fix in SkyN3t itself

These results also expose a platform-level weakness:

- heuristics can say "96" or "100"
- but semantic contradictions still collapse the run to 49

Recommended follow-up in SkyN3t:

1. add a **pre-review consistency gate** that explicitly compares:
   - architecture vs scaffold
   - README vs actual ports/runtime
   - tech_stack.json vs actual code
   - packaging artifacts vs real stack

2. downgrade heuristic completeness when:
   - major declared stack files are missing
   - dead stubs are present
   - entrypoint ignores the designed component tree

3. add reviewer warnings for:
   - unused backend deps causing false fullstack detection
   - dead `Settings.jsx` / stub components
   - product-title/template leftovers

That would catch these projects earlier and make the failure mode more actionable than "everything looked complete until the LLM dropped it into no-go."
