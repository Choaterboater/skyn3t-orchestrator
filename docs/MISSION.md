# SkyN3t — Mission

A self-healing, self-learning, autonomous multi-agent system that talks to multiple LLMs, has conversations between agents to prevent hallucinations, and builds real, runnable programs.

## What we are building toward

**A program that takes a brief and produces a real, working program — not a markdown summary, not a fake demo.**

The bar: type "homelab dashboard integrating Sonarr, Radarr, Sonos, Emby, Docker" → get a Vite+React app with real API calls (`fetch('http://sonarr.local:8989/api/v3/queue', { headers: { 'X-Api-Key': env.SONARR_API_KEY }})`), real auth, real error handling. Run `npm install && npm run dev` and see live data.

If the system can't do that, it has not met the bar.

## The five non-negotiables

These are the qualities every decision gets weighed against. If a piece of work doesn't strengthen one of these, it doesn't belong on the path.

### 1. Multi-LLM, not single-vendor

Different agents on different backends — Designer on Opus, CodeAgent on Codex, Research on Copilot, Reviewer on Sonnet. No agent should be locked to a model. Switching backends is a config edit, not a code change.

Today: `data/agent_overrides.json` wires this. Working.

### 2. Agents have conversations

Linear handoff is not enough. Designer proposes → Reviewer critiques → Designer revises. CodeAgent writes → Reviewer pushes back → CodeAgent fixes. **Cross-model debate** — one LLM proposes, a different LLM critiques. This is the anti-hallucination layer.

Today: missing. Strictly linear pipeline. **#1 build priority.**

### 3. Real memory, no amnesia

Past projects, past failures, past lessons all stay queryable. Skills library, build pattern scoreboard, RAG, Cortex history, lesson attribution. The system should remember "I tried this last week and it didn't work" without being told.

Today: components exist but underused. Skills library has 2 entries. Build pattern scoreboard empty. **Needs to actually record signals.**

### 4. Self-healing

Failed builds become fix loops. Failure patterns become Cortex proposals. The system doesn't need a human to notice it's been failing.

Today: fix loop exists, BuildVerifier too lenient (it passed a build with a broken import). **Needs honest verification.**

### 5. Self-learning

Every successful build teaches the system. Skills library captures what worked. Build pattern scoreboard remembers which file shapes succeed per stack. The system gets better with each project, not just per-conversation.

Today: code exists, signal pipeline broken. Builds finish without recording success/failure to the scoreboard. **Fix the signal path.**

## What we are NOT

- A markdown generator
- A demo builder
- A chat wrapper
- A one-shot prompt → code tool
- A clone of any existing thing

We compete with Hermes Agent, OpenClaw, Paperclip. We match their features (skills, ACP, isolated subagents, Docker backend, persistent memory, messaging adapters) and add ours (inter-agent conversation, multi-LLM debate, brutal verification, real autonomy).

## Subscription-based — no token panic

Backends are subscription-billed CLIs (claude_cli, copilot_cli, kimi_cli) and direct API. We don't care about per-call token caps — we care about quality. 8000 tokens per file is fine. 16000 is fine. If a file needs more, we give it more.

## The honest gap matrix

| Capability | Status | What's missing |
|---|---|---|
| Multi-LLM routing | ✅ | None — works |
| Agents conversing | ❌ | Entire feature |
| Anti-hallucination via cross-model debate | ❌ | Depends on conversations |
| Real memory | ⚠️ | Components exist, signal recording broken |
| Self-healing (fix loop) | ⚠️ | Loop exists, verifier too lenient |
| Self-learning | ⚠️ | Code exists, scoreboard empty |
| Self-modification (Cortex) | ✅ | Works |
| Real integrations in builds | ⚠️ | Just fixed planner+research+codeagent, needs test |
| Build verification | ⚠️ | Currently passes fake demos |
| Hermes feature parity | ✅ | 13/18 messaging platforms is the only gap |

## The build order

1. **Verify pipeline fixes work** — re-run homelab brief, check for real API calls (test, not code work).
2. **Make BuildVerifier honest** — `vite build`, `pytest`, headless render. Currently it passes builds that don't run.
3. **Wire the scoreboard signal** — every passed/failed build records `(stack, shape, verdict)`. Today this never fires.
4. **Inter-agent conversation** — Designer ↔ Reviewer iterate. CodeAgent ↔ Reviewer iterate. Cross-model debate.
5. **Brain map UI** — visualize the conversations once they exist. (Has been tried, got uglier each rev. Park until #4 lands.)
6. **Memory expansion** — RAG-backed "I tried this before" recall in agent prompts.

Everything else is below the line until these land.

## The anti-list — what we will not do

- Add more SPA pages
- Rewrite working code that serves the mission
- Bigger swarms reading entire codebases as a quality exercise
- Folder-level reorganization beyond what's already done
- Token-spend on debugging that's already been done (read git log + this file before re-asking)

## Repo layout (current, clean as of 2026-05-11)

```
~/Documents/skyn3t/
  repo/                 ← source code (this directory)
  Projects/             ← Studio build outputs
~/Documents/kimi/jarvis → symlink to repo/ (back-compat for open editors)
```

Inside `repo/`:

```
skyn3t/         ← Python package (orchestrator, agents, intelligence, cortex, RAG, web)
  web/ui/       ← Vite+React SPA dashboard
data/           ← runtime state (skills, proposals, vector_db, agent_overrides, scoreboard)
tests/          ← test suite
scripts/        ← restart-backend.sh and friends
docs/           ← this file lives here
_scratch/       ← gitignored, for stray outputs that escape (should stay empty)
```

Stray outputs at repo root were a leak. As of 2026-05-11 every agent uses
`BaseAgent.resolve_artifact_dir()` which sandboxes writes under the
configured `projects_dir`, never CWD.
