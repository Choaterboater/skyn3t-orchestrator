# SkyN3t — The 10× Plan (beat Hermes)

> Written 2026-06-11, after Phases 0–4 landed on `main`. Grounds the strategy in
> a live read of Hermes Agent (NousResearch, v0.16.x) + `docs/CONTINUE.md`.

## The thesis: don't out-Hermes Hermes — out-*build* it

Hermes Agent is a **general, server-resident assistant**: it lives on your box,
talks over ~18 channels, runs tasks in 6 backends (local/Docker/SSH/Singularity/
Modal/Daytona, with serverless hibernation), auto-writes `agentskills.io` skills,
schedules NL cron through a gateway, spawns isolated subagents, and drives a
browser. It is excellent at *doing tasks* and *remembering*.

**It does not autonomously design, build, verify, and ship real runnable
applications.** That is SkyN3t's moat. "10× better than Hermes" therefore means
two things, in priority order:

1. **MOAT (10× the builder):** make the thing Hermes can't do *dramatically*
   smarter — autonomously produce apps that actually work, look great, and get
   better every run. This is where we win 10×, not 1.1×.
2. **PARITY (close the table stakes):** match the assistant features Hermes has
   so SkyN3t is also a great always-on agent, not just a build engine.

What we already lead on (keep widening): Studio → runnable `scaffold/`, the
verification sandwich (contract → build → boot → integration → **functional +
visual gates** as of Phase 3), cross-model critique, the Cortex self-learning
flywheel, GitHub scout, PackagingAgent (now PWA/desktop/Capacitor), ACP server.

---

## Track M — Make the builder 10× smarter (the moat)

| # | Capability | Why it's 10× | Where |
|---|---|---|---|
| M1 | **Multi-LLM debate per stage** (not just critique) | The mission's anti-hallucination layer: N models propose → cross-examine → vote → synthesize. Turns "one model guesses" into "a panel converges." | `studio/runner.py`, new `agents/debate.py`, reuse `model_tournament` |
| M2 | **Wire the dead A2A conversation layer** | Designer⇄Reviewer⇄CodeAgent *converse* until convergence instead of linear handoff. The audit found `spawn_subordinate`/`delegate_task`/`on_message` are dead code — activate them. | `core/orchestrator.py`, `core/messaging.py`, `core/agent.py` |
| M3 | **Reflective self-correction** | On failure, the system reasons about *why* and rewrites its own plan/prompts (meta-learning), not just blind `-retry`. Feeds `reflection.py` → planner. | `intelligence/reflection.py`, `studio/planner.py` |
| M4 | **Predictive model routing ("auto best for the job")** | Use routing observations + tournament results to pick the best/cheapest model per (stack, stage, brief-feature) automatically. The mission's "auto mode." | `core/model_router.py`, `intelligence/routing_recommendations.py` |
| M5 | **Autonomous skill synthesis + `agentskills.io` hub** | After a winning build, distill a *new reusable skill* automatically; import/export against the open standard (Hermes-compatible) so skills are shareable. | `intelligence/skill_library.py`, `intelligence/skills_hub.py` |
| M6 | **Broader app types + build integrations** | Images/sprites/UI assets (Replicate/SDXL), Figma/Penpot design import → tokens, real auth/payments/email scaffolds, native Swift path beyond Capacitor. | `agents/designer.py`, new `agents/asset_agent.py`, `stack_templates.py` |
| M7 | **Curiosity-driven goal selection** | The system decides *what to build/improve next* from GitHub trends + its own gap analysis, not just a seeded brief queue. Deepen the existing curiosity loop. | `cortex/curiosity.py`, `cortex/competitive_intel.py` |

## Track P — Close Hermes's leads (parity → surpass)

| # | Capability | Hermes today | Where |
|---|---|---|---|
| P1 | **Serverless / remote execution backends** | local/Docker/SSH/Singularity/Modal/Daytona | new `intelligence/backends/` (RemoteBackend protocol: Modal, Daytona, E2B, SSH) alongside our Docker pool |
| P2 | **Messaging channel parity (~13 → 22+)** | Telegram/Discord/Slack/WhatsApp/Signal/Email/SMS/Matrix/Mattermost/HomeAssistant/DingTalk/Feishu/WeCom/iMessage/WeChat/… | `integrations/messaging.py` adapters |
| P3 | **NL cron + gateway delivery** | natural-language cron → gateway, unattended | extend `agents/scheduler_agent.py` + a delivery gateway |
| P4 | **Browser automation backend** | Browserbase / Browser Use / local CDP | new `agents/browser_agent.py` (Playwright MCP already available) |
| P5 | **First-run install wizard + TUI + desktop** | one-line install, desktop app, TUI | extend Phase-4 `SettingsPage` into a true wizard; Tauri shell; rich CLI/TUI |

---

## What "10×" looks like (measurable)

- **First-attempt build success rate** (per stack) trending toward Hermes-impossible
  territory because debate + reflection + predictive routing compound (Phase-2
  metric already wired — make the number go up).
- **% of generated apps that pass the functional + visual gate** approaching 100
  (Phase-3 gates exist — debate/reflection raise the input quality).
- **Zero stub deliveries**, real persistence + tests in every data app.
- **Skill reuse rate**: each build measurably benefits from a prior build's
  distilled skill (M5), and skills interop with the open standard.
- **Reach**: same agent answers on 22+ channels, runs serverless on Modal/Daytona,
  and schedules itself — so it's also a great always-on assistant.

## Suggested sequencing

1. **Track M first** (M1 debate, M2 conversations, M3 reflection, M4 routing) —
   this is the 10× moat and reuses subsystems we just fixed in Phases 2–3.
2. **M5 skills hub + M6 app breadth** — compounding quality + reach into new app
   types (the "build photos/sprites/UI/mobile" asks from the mission).
3. **Track P** — backends (P1), channels (P2), cron/gateway (P3), browser (P4),
   wizard/desktop (P5) — assistant parity so SkyN3t is also always-on.

## Open questions for the owner

1. **Lead track?** Moat-first (smarter builder) vs Parity-first (channels/backends)
   vs both in parallel.
2. **Cost ceiling for debate** — N-model debate multiplies tokens per stage; cap N
   and gate it to high-stakes stages (architect/review/code) only?
3. **Which integrations to fund** — Replicate/image-gen, Figma/Penpot, Modal/Daytona
   accounts, messaging API keys: which do you actually have/ want first?
4. **Self-modification scope** — M2/M3/M7 let the system rewrite its own plans and
   pick its own goals. Keep approval-gated, or allow autonomous above a threshold
   (consistent with the Phase-2 graduation decision)?
