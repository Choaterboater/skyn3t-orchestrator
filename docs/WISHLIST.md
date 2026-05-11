# Wishlist — parked ideas

Things that matter but are not the current build priority. Each item
has a "why parked" so it doesn't get pulled in by accident.

## Brain map UI — visualize agent conversations

**What:** A live network graph of which agents are talking, with edges
highlighting the active message flow. Like a NOC for the swarm.

**Why parked:** Tried multiple times, got uglier each iteration. The
underlying feature (agents actually conversing) doesn't exist yet, so
the map was always animating a fake graph. Park until inter-agent
conversation lands.

**When to revisit:** After conversation feature is real and useful.

## Messaging platform parity with Hermes (5 more channels)

**What:** Add WeCom, Lark, Line, KakaoTalk, Telegram bot (if not done).
Get to 18 to match Hermes.

**Why parked:** We have 13 already. The gap isn't blocking any user
workflow. Adding more channels is volume work, not strategic.

**When to revisit:** Only if a user explicitly needs one.

## Real autonomy loops

**What:** SchedulerAgent runs autonomous cycles — overnight skill
curation, weekly self-audit, monthly architecture review.

**Why parked:** SchedulerAgent exists, plumbing is there, but
inter-agent conversation is the more useful unlock first. Autonomy
without good conversation amplifies bad decisions.

**When to revisit:** After conversation + brutal verification land.

## Docker subagent runners by default

**What:** Move from subprocess isolation to Docker by default for
CodeAgent runs.

**Why parked:** Adds a Docker dependency for casual users. Current
subprocess isolation is good enough for now.

**When to revisit:** When users hit a real sandboxing failure.

## Code audit (the "read everything" ask)

**What:** Systematic file-by-file review of the codebase.

**Why parked:** History says this burns tokens for low yield. The
audit by symptom approach (find specific bug → fix it) has produced
better results than swarm-reading.

**When to revisit:** Audit specific subsystems (one at a time) when
they're suspected of bugs.

## Web search backend for ResearchAgent

**What:** Real web fetch tool (Brave/Tavily/Exa) for ResearchAgent
to look up API docs, GitHub repos, dashboard examples.

**Why parked:** Adds an external API dependency + cost. LLM-grounded
research is "good enough" for now. **However:** if the homelab build
test (current priority) shows research is still weak, this gets
promoted immediately.

**When to revisit:** Right after the homelab build test. Likely soon.

## iOS / Swift / mobile builds

**What:** Studio templates for iOS app, Android app, React Native.

**Why parked:** Web + CLI + API stacks cover ~80% of likely briefs.
Mobile adds a lot of toolchain complexity (Xcode, signing, simulators).

**When to revisit:** When a user actually briefs a mobile app and
the system has matured on web/server first.
