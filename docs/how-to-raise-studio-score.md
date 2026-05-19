# How to Raise Studio Review Scores

Date: 2026-05-19

This guide explains how SkyN3t's reviewer currently scores Studio runs, why projects get stuck at low numbers like **49** or low-50s, and what to change in the generated project to raise the score on the next run.

This is based on the current reviewer implementation in:

- `skyn3t/agents/reviewer.py`
- recent saved Studio reviews in `~/Documents/Skyn3t/Projects/*/review.md`

## Short version

If you want the score to go up, do these first:

1. **Make the shipped app match the written docs**
   - README, architecture, ports, Docker, runtime, and actual code must agree

2. **Do not imply a backend/fullstack app unless it really exists**
   - fake backend stories are heavily punished

3. **Make the real entrypoint use the real designed components**
   - if the polished components are never rendered, the reviewer treats them as unused theater

4. **Remove dead stubs and template leftovers**
   - wrong product title
   - dead `Settings.jsx`
   - stub components
   - unused server dependencies

5. **If packaging is enabled, ship the right packaging artifacts**
   - web: usable Settings + `useConfig`
   - server: working Dockerfile + compose + `.env.example`
   - fullstack: both layers plus real compose wiring

## How the score is built

The reviewer combines three inputs:

1. **Heuristic score**
   - file presence
   - missing expected artifacts
   - short stub docs
   - TODO/FIXME markers
   - consistency checks on things like `palette.json` and `tech_stack.json`

2. **LLM score**
   - semantic review of completeness, correctness, consistency, and packaging
   - this is where contradictions get punished hard

3. **Packaging score**
   - only when packaging is enabled for the run
   - graded from 0-10, then blended into the final score

Current blend:

- with packaging enabled:
  - `0.54 * llm + 0.36 * heuristic + 0.10 * packaging`
- with packaging disabled:
  - `0.60 * llm + 0.40 * heuristic`

## Why 49 happens so often

The score logic uses two numbers:

1. **Displayed blended score**
2. **Verdict score**

The verdict score is capped by the LLM bucket:

- if `llm_score < 50`, verdict stays in `no-go`
- if `50 <= llm_score < 75`, verdict stays in `go-with-fixes`
- only `llm_score >= 75` can become `go`

So the important rule is:

> If the LLM thinks the project is fundamentally broken, heuristic file-completeness cannot rescue it.

That is why you can see:

- high heuristic numbers like `96` or `100`
- but still get a `49`-ish result or a weak verdict

## Why you may see "49 / 54"

You may now see a run where:

- the **displayed score** is in the 50s
- but the **verdict** is still effectively stuck in the lower bucket

That happens because the reviewer now preserves the higher blended score for visibility, while still using the LLM bucket to determine the verdict. In practice:

- a run can improve from "true disaster" to "less bad"
- but it still will not graduate to a better verdict until the **LLM score itself** crosses the next bucket

So if you see something like:

- heuristic high
- blended score in the mid-50s
- verdict still weak

the fix is not "add more files." The fix is "raise the LLM's opinion of the project's coherence."

## What the LLM reviewer punishes most

Based on the recent low-score Studio runs, the LLM reviewer is especially sensitive to these failures:

### 1. Contradictory stack story

Examples:

- README says backend runs on `:8000`
- `vite.config.js` proxies to `:3100`
- Docker runs Python
- `tech_stack.json` says Express
- actual repo has no backend

This is one of the fastest ways to tank the LLM score.

### 2. Designed system is not the real app

Examples:

- polished components exist in `src/components/`
- but `App.jsx` is still a one-off localStorage demo
- intended hooks/components are never imported or rendered

The reviewer reads this as "the designed app was not actually shipped."

### 3. Fullstack claims without a real backend

Examples:

- architecture documents API endpoints
- `useConfig`/proxy/auth imply a backend
- Docker/compose imply a backend
- no backend files actually exist

This gets punished as both correctness and packaging failure.

### 4. Broken brand/design consistency

Examples:

- `brand.md` says light UI, scaffold is dark
- palette tokens collapse to the same few colors
- `tokens.css`, `tokens.json`, `palette.json`, logo, and app styles disagree

This often drags down the consistency score sharply.

### 5. Dead stubs and leftovers

Examples:

- `Settings.jsx` exists but has no fields and does nothing
- `WeekStrip.jsx` returns `null`
- product title is from an unrelated template
- dead dependencies imply non-existent backend code

These tell the reviewer the project is unfinished or template-corrupted.

## The five fastest ways to raise the next score

## 1. Pick one product shape and commit to it

Before the next run, decide:

- **frontend-only**
- **server-only**
- **real fullstack**

Then make all artifacts agree with that choice.

### If frontend-only

Remove or avoid:

- backend API claims in README
- Docker backend story
- fake auth/server docs
- server dependencies that are never used

### If fullstack

Add for real:

- backend entrypoint
- backend dependencies/runtime files
- consistent port config
- working compose story
- actual routes/services the frontend depends on

## 2. Make the entrypoint real

The reviewer cares about what the user actually launches.

That means:

- `src/main.*`
- `src/App.*`
- router wiring
- actual rendered page tree

If your best components are in the repo but never rendered, the score will still stay low.

### Checklist

- `App.jsx` imports the intended components
- the main happy path is reachable in the UI
- placeholder/demo UI is removed
- dead alternate implementations are deleted

## 3. Make docs and implementation match exactly

Reviewers reward coherence more than verbosity.

### Must match:

- app name/title
- framework
- backend language
- ports
- startup steps
- deployment story
- auth story
- data/storage story

### Quick rule

If README says "run X" or "this app uses Y", the repo should make that obviously true.

## 4. Clean up packaging before review

Packaging contributes directly to the score and also influences the LLM's trust in the project.

### Web projects should have:

- `scaffold/src/Settings.jsx`
- `scaffold/src/hooks/useConfig.js`
- working first-run config flow if config is required

### Server projects should have:

- `Dockerfile`
- `docker-compose.yml` or `compose.yaml`
- `.env.example`

### Fullstack projects should have:

- working frontend layer
- working backend layer
- compose wiring for both
- consistent backend URL/defaults

### Important

Do not ship packaging for a stack you do not actually have.

A wrong Dockerfile is worse than no Dockerfile.

## 5. Delete dead files instead of hoping they are ignored

Low-score reviews repeatedly call out files that are technically present but semantically wrong.

Delete or implement:

- stub components
- dead settings pages
- fake backend deps
- wrong titles
- old template logos/copy
- unused docs that contradict the scaffold

## How to raise each score component

## Raise the heuristic score

Do this:

- include all expected core artifacts for the stages you ran
- avoid very short stub markdown files
- remove TODO/FIXME/TBD markers
- ensure `tech_stack.json` has expected keys
- ensure palette JSON is valid

This helps, but by itself it is not enough.

## Raise the packaging score

Do this:

- only if packaging is enabled, make sure the generated packaging matches the actual app family
- include real README + `.gitignore`
- include the right per-family artifacts

This is useful but only contributes part of the result.

## Raise the LLM score

This is the highest leverage path.

Do this:

1. remove contradictions
2. remove fake/fullstack theater
3. make the visible app match the intended design
4. make every run instruction actually work
5. make the architecture describe what was shipped, not what was imagined

If the LLM score stays below 50, the run will keep feeling stuck in the bottom bucket.

## Pre-review checklist for Studio runs

Use this before treating a project as "done":

### Product coherence

- [ ] App title matches the actual product
- [ ] README describes the app that actually exists
- [ ] Architecture matches the shipped implementation
- [ ] `tech_stack.json` matches the real runtime and code

### UI coherence

- [ ] Entry point renders the intended components
- [ ] Placeholder/demo UI is removed
- [ ] Brand tokens and actual styles agree
- [ ] No obviously broken borders/colors/contrast

### Stack honesty

- [ ] If frontend-only, no fake backend claims
- [ ] If fullstack, the backend actually exists
- [ ] Ports are consistent across README, config, proxy, and compose

### Packaging

- [ ] Docker/compose files only exist if they are runnable
- [ ] `.env.example` reflects real config
- [ ] Settings/config UI is real if the app needs runtime config

### Cleanup

- [ ] No wrong template names or titles
- [ ] No stub components
- [ ] No dead dependencies implying missing subsystems
- [ ] No obviously unused placeholder screens

## Recommended process change for better future runs

If you want Studio scores to rise more reliably, the best workflow change is:

1. **Generate**
2. **Run a coherence pass**
3. **Delete/repair contradictions**
4. **Only then review**

That coherence pass should explicitly compare:

- README vs actual startup path
- architecture vs actual code
- packaging files vs actual stack
- component plan vs actual entrypoint
- brand tokens vs rendered UI

## Bottom line

To make the score higher:

- stop optimizing for "more files"
- optimize for **one believable, internally consistent shipped product**

That is what moves the LLM score upward, and the LLM score is what ultimately determines whether the run escapes the low-score bucket.
