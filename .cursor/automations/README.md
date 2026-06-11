# SkyN3t Cursor Automations

## SkyN3t continuous improvement

Periodic Cloud Agent that keeps improving **this repo** while the autonomous fleet improves **generated projects**.

### Enable in Cursor UI

1. Open **Automations** in Cursor (or ask the agent: "open the SkyN3t continuous improvement automation").
2. **New automation** → import values from `skyn3t-continuous-improvement.json` (prefill) or copy the schedule + prompt below.
3. Set **Git repo** to `Choaterboater/skyn3t-orchestrator`, branch `main` (or your working branch).
4. Enable **Cloud Agent** if you want unattended runs.
5. Save and enable the automation.

### Schedule (recommended)

| Preset | Cron | When |
|--------|------|------|
| Weekday mornings | `0 9 * * 1-5` | Mon–Fri 09:00 |
| Twice daily | `0 9,17 * * *` | 09:00 and 17:00 |
| Hourly (aggressive) | `0 * * * *` | Every hour |

### What each run does

1. Read `docs/CONTINUE.md` next priorities.
2. Check `data/cursor_tasks.json` for flywheel-queued work.
3. Run `ruff check skyn3t tests` and `pytest` subset (fleet, improvement, cheap_smart).
4. `curl` fleet + improvement APIs when the web server is up.
5. Implement **one** small shippable improvement (UI, fleet bug, Hermes gap).
6. Run tests again — **open a PR only if tests pass**.

### Manual trigger

In Cursor chat:

```
Read docs/CONTINUE.md and data/cursor_tasks.json. Process the highest-priority
cursor task. Run ./scripts/cursor_improve.sh before and after. No PR unless tests pass.
```

### Dual loop reminder

- **Fleet** (`SKYN3T_AGENT_FLEET_SIZE`, `SKYN3T_AUTONOMOUS_BUILDS`) → Studio builds in `PROJECTS_DIR`.
- **Cursor automation** → `skyn3t/` repo quality, dashboard, cortex wiring.
