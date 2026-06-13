# SkyN3t Deployment Notes — Single-Leader + External Watchdog

## Single-process architecture

SkyN3t is currently a **single-leader, single-process** system:

* The event bus, fleet registry, collective consciousness, token tracker, and
  running task state are all in-memory singletons.
* There is no distributed consensus, shared queue, or replicated state.
* Running more than one uvicorn worker or more than one SkyN3t process against
  the same `data/` directory will split brain: each process sees a different
  fleet, publishes duplicate events, and overwrites the others' SQLite/Chroma
  state.

**Deploy exactly one SkyN3t process at a time.** If you need horizontal
scaling, put a stateless load balancer in front of *multiple independent*
SkyN3t instances, each with its own `DATA_DIR` and no shared files.

## External watchdog

The built-in `never_stop` loop runs inside the same process it guards. A
segfault, OOM kill, or hard crash will take both the server and the watchdog
down. Use an external supervisor that restarts the process when the health
check fails.

`scripts/healthcheck.sh` probes `/health` and exits non-zero when the server is
down or returns an error. Use it with:

* **systemd**: `ExecStart=` the uvicorn command, `Restart=on-failure`, and
  optionally a `ExecStartPost=` / `ExecStopPost=` that pages you.
* **launchd** on macOS: set `KeepAlive` with a `ThrottleInterval` and point
  `WatchPaths` or a periodic `StartInterval` at the health check.
* **Docker**: add a `HEALTHCHECK` that runs the script and a restart policy
  such as `unless-stopped`.

Example Docker Compose health check:

```yaml
services:
  skyn3t:
    image: skyn3t:latest
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "bash", "/app/scripts/healthcheck.sh"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

## Fleet-aware supervisor (moat plan Phase 3)

For autonomous fleet runs, probe **`/api/fleet/status`** in addition to `/health`
so a hung cortex (web up, fleet dead) still triggers a restart.

```bash
# scripts/healthcheck_fleet.sh — exit non-zero when fleet API is unhealthy
curl -sf "http://127.0.0.1:6660/api/fleet/status" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok', True) else 1)"
```

### systemd unit (Linux)

```ini
[Unit]
Description=SkyN3t orchestrator
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/skyn3t/repo
Environment=SKYN3T_NEVER_STOP=1
ExecStart=/opt/skyn3t/repo/.venv/bin/python -m skyn3t.cli.main start --host 127.0.0.1 --port 6660
ExecStartPost=/bin/sleep 5
Restart=on-failure
RestartSec=10
# Optional external wrapper instead of direct ExecStart:
# ExecStart=/opt/skyn3t/repo/scripts/never_stop.sh

[Install]
WantedBy=multi-user.target
```

Pair with a timer or `WatchdogSec` using `scripts/healthcheck_fleet.sh`.

### launchd (macOS)

Use `scripts/never_stop.sh` as the `ProgramArguments` entry and set
`KeepAlive` + `ThrottleInterval` 30. Point a `StartInterval` job at
`scripts/healthcheck_fleet.sh` to restart when fleet status fails.

### Process wrapper

`scripts/never_stop.sh` restarts the web server when port 6660 stops
listening. It complements — but does not replace — an external supervisor
for OOM/segfault kills.

## Credential hygiene

Keep `.env` at mode `600`, store production secrets in a keychain / secret
manager, and rotate any token that has ever been committed or world-readable.
