# SkyN3t — Status

SkyN3t is a solo-built multi-agent code-generation orchestrator. It turns a short
build request into a working full-stack app by running a single core loop:
**brief -> plan -> generate -> verify -> ship**. Agents are coordinated through a
tier-aware model router and a shared learnings store, with autonomous improvement
loops kept behind an approval gate.

## Current state (2026-06-13)

- **Budget: free-only.** The OpenRouter key has a $0 paid budget. The system runs
  entirely on the free model catalog via `SKYN3T_FREE_ONLY=1`. No paid model calls
  are made.
- **Focus: hardening the core build loop.** The brief -> plan -> generate -> verify
  -> ship pipeline is being made reliable enough to ship full-stack
  (React + FastAPI) builds end-to-end on free models — not just plan or scaffold,
  but produce a verified, runnable app.
- Breadth work is deprioritized; the goal is one reliable core loop, not more
  surface area.

## Where things live

- `skyn3t/studio/runner.py` — the build pipeline (drives brief -> plan -> generate
  -> verify -> ship).
- `skyn3t/agents/code_agent.py` — code generation.
- `skyn3t/core/model_router.py` — tier routing / model selection (honors
  `SKYN3T_FREE_ONLY`).
- `skyn3t/cortex/` — autonomous improvement loops (approval-gated).
- `skyn3t/intelligence/learnings_store.py` — distilled knowledge reused across
  builds.

## Archived plans

Old planning and audit docs live in [`docs/archive/`](docs/archive/). They are
historical only — this file (`STATUS.md`) is the single living status doc. Do not
re-reference archived plans as current direction.
