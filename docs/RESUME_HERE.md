# Resume notes — 2026-05-11 ~13:01

## Where we left off

In the middle of testing the homelab-dashboard build pipeline. v11
was just launched and is mid-flight when the PC needed to restart.

**Last build state (will be marked `interrupted` after PC reboot):**
- `homelab-dashboard-v11` running with all today's fixes active
- Was the integration test for the reviewer-cap fix (12KB per file
  vs 1500 chars before — kimi was hallucinating reviews because it
  couldn't see the actual App.jsx code)

## What everything is at right now

All today's work is **committed and pushed** to branch
`skyn3t/auto/ui-rebuild`. The remote is up to date. Nothing to lose.

**Repo lives at:** `~/Documents/skyn3t/repo/`
(Old `~/Documents/kimi/jarvis` is a symlink for back-compat.)

**Project builds land at:** `~/Documents/skyn3t/Projects/`
- `homelab-dashboard-v8` is the best baseline so far (real 23KB App.jsx)
- `homelab-dashboard-v9` (46KB App.jsx, interrupted)
- `homelab-dashboard-v10` (37KB App.jsx, reviewer scored 43 because it could only see first 1500 chars)
- `homelab-dashboard-v11` (will be marked interrupted after reboot)

## To resume cleanly

```bash
# 1. Start backend
cd ~/Documents/skyn3t/repo
bash scripts/restart-backend.sh

# 2. Start Vite SPA (in another terminal)
cd ~/Documents/skyn3t/repo/skyn3t/web/ui
npm run dev

# 3. Browser
open http://localhost:5173/studio
```

Backend listens on `127.0.0.1:6660`. SPA at `http://localhost:5173`.

## The next test to run

Re-launch the homelab brief (v12) to verify the reviewer-cap fix
produces a real score. v10's 43/100 was a hallucination — App.jsx
is genuinely good (37KB with real `fetch`/env-var/integration code).

Curl form:

```bash
curl -s -X POST http://127.0.0.1:6660/api/studio/start \
  -H "Content-Type: application/json" \
  -d '{
    "template": "auto",
    "slug": "homelab-dashboard-v12",
    "brief": "Build a homelab dashboard that integrates with real services: Sonarr (port 8989), Radarr (port 7878), Prowlarr (port 9696), qBittorrent (port 8080), Emby (port 8096), and Sonos via the SoCo Python library or sonos-http-api. Plus Docker container monitoring via the Docker socket. The dashboard must make real API calls (not hardcoded demo arrays), read credentials from env vars (SONARR_API_KEY, RADARR_API_KEY, EMBY_API_KEY, QBIT_USER, QBIT_PASS, etc.), and show: download queues with progress, currently-playing Emby sessions, container CPU/memory, Sonos now-playing per zone with controls. Stack: Vite + React. Must be a real, runnable program — do not use mock data."
  }'
```

Expected runtime: ~22 min (matching v10). Expected score with the
reviewer fix: ≥75 since the scaffold is real and reviewer can now
actually read it.

## Today's mission progress (one-screen view)

What we shipped today, all live on next restart:

1. ✅ Planner forces ResearchAgent on integration-naming briefs
2. ✅ Research produces real API specs (MCP web search, not bullets)
3. ✅ Research caches specs to skills, recalls them on future builds
4. ✅ CodeAgent reads prior artifacts (research.md, architecture.md, etc.)
5. ✅ CodeAgent system prompt demands real integrations, not demos
6. ✅ Cross-model retry actually works (skip_backends bug fixed in
   research + code)
7. ✅ Inter-agent critique + revise (kimi reviews opus/codex output)
   on lightweight stages (research/architect/designer/writer)
8. ✅ Code-stage critique skipped (would re-run entire scaffold)
9. ✅ Multi-LLM diversity: opus×3 + kimi×3 + codex + gpt-5.4 spread
10. ✅ 8 hand-curated seed skills + 4 more agents reading them
11. ✅ Stage timeouts: 1800s for code/research/reviewer, 300s otherwise
12. ✅ LLM call timeout: 600s
13. ✅ Build-pattern scoreboard records timeouts and exceptions, not
    just verifier results
14. ✅ Token tracker subscribes to LLM_EXCHANGE, exposes per-agent
    + per-project rollups via /api/usage/* endpoints
15. ✅ Studio + Agents pages show token columns
16. ✅ Reviewer cap fix: PER_FILE_CAP 1500 → 12000, TOTAL 30K → 100K

## Open questions for the next session

- v11 outcome (probably needs to be re-run as v12 since restart
  killed it)
- Is the reviewer fix enough to get a real high score, or do we
  also need to address that the reviewer is using truncated content
  via `_extract_quality_candidate` path elsewhere?
- BuildVerifier still hasn't recorded any results to the scoreboard
  — that's a separate path from the stage-failure record I added
- CodeAgent per-file revision is on the wishlist (enabling code-stage
  critique without 40min wall-time penalty)

## What NOT to do on resume

- Don't re-do today's work. Everything is committed.
- Don't rebuild the project. The pipeline produces real code now.
- Don't add new SPA pages or features without an explicit ask.
- Don't restart backends without checking what's running first
  (use `pgrep -f "skyn3t start"`).

## Files worth reading first on resume

1. `docs/MISSION.md` — the goal as written down 2026-05-11
2. `docs/WISHLIST.md` — parked ideas with "why parked"
3. This file
4. `git log --oneline -30 skyn3t/auto/ui-rebuild` — recent commits
