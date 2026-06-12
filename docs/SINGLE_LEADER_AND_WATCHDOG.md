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

## Credential hygiene

Keep `.env` at mode `600`, store production secrets in a keychain / secret
manager, and rotate any token that has ever been committed or world-readable.
